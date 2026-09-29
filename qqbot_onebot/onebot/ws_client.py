"""OneBot v11 反向 WS 客户端(对 BotShepherd / nonebot 伪装成 napcat 类实现端).

- 握手头 X-Self-ID / X-Client-Role: Universal / User-Agent / 可选 Authorization(BS 转发下游).
- 连上立刻发 lifecycle connect: BS 收到首帧才去连下游.
- echo 必须原样返回, BS 靠它路由响应.
- BS 会主动发 get_status(echo=status_check_*) 做在线检查.
- 心跳 30s(BS 靠 WS ping 保活, 心跳发了无害).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import websockets

from .. import __version__
from .events import heartbeat_event, lifecycle_event

if TYPE_CHECKING:
    from ..core.bot import BotInstance

logger = logging.getLogger("qqbot.oblink")

HEARTBEAT_INTERVAL = 30
RECONNECT_DELAY = 5


class OneBotLink:
    def __init__(self, bot: "BotInstance", url: str, access_token: str = ""):
        self.bot = bot
        self.url = url
        self.access_token = access_token
        self._ws: websockets.ClientConnection | None = None
        self._closed = False
        self._connected_at: float = 0
        self._send_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)

    def snapshot(self) -> dict:
        return {
            "url": self.url,
            "connected": self._ws is not None,
            "connected_at": int(self._connected_at),
        }

    def send_text(self, payload: str) -> None:
        if self._ws is None:
            return  # 未连接时丢弃事件(OneBot 无离线补投语义)
        try:
            self._send_queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("[%s] send queue full, dropping event", self.url)

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            await self._ws.close()

    async def run(self) -> None:
        while not self._closed:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[%s][%s] ws error: %s",
                               self.bot.appid, self.url, exc)
            self._ws = None
            if self._closed:
                return
            await asyncio.sleep(RECONNECT_DELAY)

    async def _run_once(self) -> None:
        headers = {
            "X-Self-ID": str(self.bot.self_id),
            "X-Client-Role": "Universal",
            "User-Agent": f"qqbot-onebot/{__version__}",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        async with websockets.connect(
            self.url, additional_headers=headers, max_size=None,
            ping_interval=20, ping_timeout=30,
        ) as ws:
            self._ws = ws
            self._connected_at = time.time()
            logger.info("[%s] connected to %s", self.bot.appid, self.url)
            # 首帧 lifecycle: BS 依赖它注册账号并连接下游
            await ws.send(json.dumps(lifecycle_event(self.bot.self_id)))

            tasks = [
                asyncio.create_task(self._pump_outgoing(ws)),
                asyncio.create_task(self._pump_heartbeat(ws)),
                asyncio.create_task(self._pump_incoming(ws)),
            ]
            try:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
            finally:
                for task in tasks:
                    task.cancel()

    async def _pump_outgoing(self, ws) -> None:
        while True:
            payload = await self._send_queue.get()
            await ws.send(payload)

    async def _pump_heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await ws.send(json.dumps(
                heartbeat_event(self.bot.self_id, HEARTBEAT_INTERVAL * 1000)
            ))

    async def _pump_incoming(self, ws) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("[%s] bad frame: %.200s", self.url, raw)
                continue
            if not isinstance(frame, dict) or "action" not in frame:
                continue
            # 并发处理动作, 避免慢 API 阻塞后续帧
            asyncio.create_task(self._handle_action(ws, frame))

    async def _handle_action(self, ws, frame: dict) -> None:
        from .actions import dispatch_action  # noqa: PLC0415 循环依赖

        action = str(frame.get("action", ""))
        params = frame.get("params") or {}
        echo = frame.get("echo")
        response = await dispatch_action(self.bot, action, params)
        if echo is not None:
            response["echo"] = echo  # 原样返回, BS 靠它路由
        try:
            await ws.send(json.dumps(response, ensure_ascii=False))
        except Exception as exc:
            logger.warning("[%s] failed to send response for %s: %s",
                           self.url, action, exc)
