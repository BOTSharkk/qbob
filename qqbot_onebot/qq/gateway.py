"""QQ 官方 WebSocket 网关客户端(webhook 之外的可选事件通道).

hello(op10) -> identify(op2, intents=GROUP_AND_C2C_EVENT|INTERACTION) ->
ready -> 心跳(op1, d=last_seq); 断线用 resume(op6) 续传.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import websockets

if TYPE_CHECKING:
    from ..core.bot import BotInstance

logger = logging.getLogger("qqbot.gateway")

# 以 nonebot-adapter-qq 为准: 群成员事件在 1<<24, 文档误写成 1<<25.
INTENT_GROUP_MEMBER = 1 << 24     # GROUP_MEMBER_ADD/REMOVE, GROUP_JOIN_REQUEST
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_INTERACTION = 1 << 26

FULL_INTENTS = INTENT_GROUP_MEMBER | INTENT_GROUP_AND_C2C | INTENT_INTERACTION
# 无权限的 intent 会被关连接(4013/4014, 不可 resume), 踩到就退回基础位
BASE_INTENTS = INTENT_GROUP_AND_C2C | INTENT_INTERACTION
INTENT_ERROR_CODES = {4013, 4014}
# 终局判决: 4914 已下架(只许沙箱), 4915 已封禁; 不重连, 自动禁用
FATAL_CLOSE_CODES = {4914, 4915}


class GatewayClient:
    def __init__(self, bot: "BotInstance"):
        self.bot = bot
        self._session_id = ""
        self._last_seq = 0
        self._intents = FULL_INTENTS

    async def run(self) -> None:
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                self.bot.gateway_connected = False
                return
            except websockets.exceptions.ConnectionClosed as exc:
                code = getattr(exc, "code", 0) or getattr(exc.rcvd, "code", 0)
                if code in FATAL_CLOSE_CODES:
                    reason = ("机器人已下架(close 4914)" if code == 4914
                              else "机器人已封禁(close 4915)")
                    logger.warning("[%s] 网关终局判决: %s, 停止重连",
                                   self.bot.appid, reason)
                    self.bot.last_error = f"gateway: {reason}"
                    self.bot.gateway_connected = False
                    self.bot._fire_fatal(f"网关 {reason}")
                    return
                if code in INTENT_ERROR_CODES and self._intents != BASE_INTENTS:
                    logger.warning(
                        "[%s] 网关拒绝 intents(close %s), 退回基础订阅 —— "
                        "群成员事件(入群/退群/入群申请)将收不到", self.bot.appid, code)
                    self._intents = BASE_INTENTS
                    self.bot.last_error = f"gateway: intents 无权限(close {code})"
                else:
                    logger.warning("[%s] gateway closed: %s", self.bot.appid, exc)
                    self.bot.last_error = f"gateway: {exc}"
            except Exception as exc:
                logger.warning("[%s] gateway error: %s", self.bot.appid, exc)
                self.bot.last_error = f"gateway: {exc}"
            self.bot.gateway_connected = False
            await asyncio.sleep(5)

    async def _run_once(self) -> None:
        url = await self.bot.api.gateway_url()
        token = await self.bot.api.get_token()
        async with websockets.connect(url, max_size=None) as ws:
            heartbeat_task: asyncio.Task | None = None
            try:
                async for raw in ws:
                    payload = json.loads(raw)
                    op = payload.get("op")
                    if payload.get("s"):
                        self._last_seq = payload["s"]

                    if op == 10:  # Hello
                        interval = (payload.get("d") or {}).get(
                            "heartbeat_interval", 45000
                        )
                        if self._session_id:
                            await ws.send(json.dumps({"op": 6, "d": {
                                "token": f"QQBot {token}",
                                "session_id": self._session_id,
                                "seq": self._last_seq,
                            }}))
                        else:
                            await ws.send(json.dumps({"op": 2, "d": {
                                "token": f"QQBot {token}",
                                "intents": self._intents,
                                "shard": [0, 1],
                                "properties": {},
                            }}))
                        heartbeat_task = asyncio.create_task(
                            self._heartbeat(ws, interval / 1000)
                        )
                    elif op == 0:  # Dispatch
                        event_type = str(payload.get("t", ""))
                        data = payload.get("d") or {}
                        if event_type == "READY":
                            self._session_id = str(data.get("session_id", ""))
                            self.bot.gateway_connected = True
                            self.bot.last_error = ""
                            logger.info("[%s] gateway ready", self.bot.appid)
                        elif event_type == "RESUMED":
                            self.bot.gateway_connected = True
                            logger.info("[%s] gateway resumed", self.bot.appid)
                        else:
                            asyncio.get_running_loop().create_task(
                                self.bot.dispatch_qq_event(
                                    event_type, data, str(payload.get("id", "")))
                            )
                    elif op == 7:  # Reconnect
                        return
                    elif op == 9:  # Invalid session
                        self._session_id = ""
                        self._last_seq = 0
                        return
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()

    async def _heartbeat(self, ws, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": 1, "d": self._last_seq or None}))
