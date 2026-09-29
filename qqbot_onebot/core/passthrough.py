"""透传: 让说 QQ 官方协议的框架(nonebot-adapter-qq/botpy/Koishi)与 OneBot 后端共用一个 bot.

- 调用: `/qqapi/` 下任意路径原样转发平台, 鉴权换成本服务的 token.
- 事件: 模拟网关(Hello/Identify/Resume…)和/或 Ed25519 签名的 webhook 回调;
  与 OneBot 同一道闸过滤.
- 透传侧发消息不经本服务记账, 与 OneBot 回复同一消息时 msg_seq 可能撞车.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from typing import TYPE_CHECKING

from ..qq.webhook import _signing_key

if TYPE_CHECKING:
    from .bot import BotInstance

logger = logging.getLogger("qqbot.passthrough")

HEARTBEAT_INTERVAL_MS = 41250
# 断线后可 Resume 的保留时长与补发上限
SESSION_KEEP_SECONDS = 600
SESSION_BUFFER = 500
WEBHOOK_TIMEOUT = 10

# 事件类型 -> intents 位; 不在表里的一律下发
_INTENT_OF = {
    "GROUP_MEMBER_ADD": 1 << 24, "GROUP_MEMBER_REMOVE": 1 << 24,
    "GROUP_JOIN_REQUEST": 1 << 24,
    "INTERACTION_CREATE": 1 << 26,
}
for _name in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE",
              "GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_MSG_REJECT",
              "GROUP_MSG_RECEIVE", "FRIEND_ADD", "FRIEND_DEL", "C2C_MSG_REJECT",
              "C2C_MSG_RECEIVE"):
    _INTENT_OF[_name] = 1 << 25

# 发出的 token -> (appid, 过期时间); 全局一张表, 靠 token 区分 bot
_issued: dict[str, tuple[str, float]] = {}


def issue(appid: str, token: str, expire_at: float) -> None:
    now = time.time()
    for key in [k for k, (_, exp) in _issued.items() if exp < now]:
        _issued.pop(key, None)
    _issued[token] = (appid, expire_at)


def appid_of_token(header: str, bots) -> str:
    """Authorization 头 -> appid(也认 bot 当前真 token); 认不出返回空串."""
    token = header.strip()
    if token[:6].lower() == "qqbot ":
        token = token[6:].strip()
    if not token:
        return ""
    hit = _issued.get(token)
    if hit and hit[1] > time.time():
        return hit[0]
    for bot in bots:
        if bot.api._token and secrets.compare_digest(bot.api._token, token):
            return bot.appid
    return ""


class GatewaySession:
    def __init__(self, intents: int):
        self.session_id = secrets.token_hex(16)
        self.intents = int(intents or 0)
        self.seq = 0
        self.buffer: deque[tuple[int, str]] = deque(maxlen=SESSION_BUFFER)
        self.queue: asyncio.Queue[str] | None = None     # 连着时才有
        self.detached_at = 0.0

    def wants(self, event_type: str) -> bool:
        bit = _INTENT_OF.get(event_type)
        return bit is None or not self.intents or bool(self.intents & bit)

    def push(self, event_type: str, data, event_id: str = "") -> None:
        self.seq += 1
        frame = json.dumps({"op": 0, "s": self.seq, "t": event_type, "d": data,
                            "id": event_id}, ensure_ascii=False)
        self.buffer.append((self.seq, frame))
        if self.queue is not None:
            try:
                self.queue.put_nowait(frame)
            except asyncio.QueueFull:
                logger.warning("透传会话 %s… 发送队列满, 丢弃事件", self.session_id[:8])

    def kick(self) -> None:
        """通知当前连接收尾; 队列满先腾一格, 免得卡住的旧连接拖累新连接/关服."""
        queue = self.queue
        if queue is None:
            return
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait("")

    def replay_after(self, seq: int) -> list[str]:
        return [frame for s, frame in self.buffer if s > seq]


class Passthrough:
    def __init__(self, bot: "BotInstance"):
        self.bot = bot
        self.sessions: dict[str, GatewaySession] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def webhooks(self) -> list[str]:
        out = []
        for item in self.bot.cfg.get("passthrough_webhooks") or []:
            url = item.get("url") if isinstance(item, dict) else item
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                out.append(url)
        return out

    @property
    def active(self) -> bool:
        return bool(self.sessions) or bool(self.webhooks)

    def snapshot(self) -> dict:
        return {"sessions": sum(1 for s in self.sessions.values() if s.queue is not None),
                "webhooks": len(self.webhooks)}

    # ---------------- 网关会话 ----------------

    def open_session(self, intents: int) -> GatewaySession:
        self._expire_sessions()
        session = GatewaySession(intents)
        self.sessions[session.session_id] = session
        return session

    def find_session(self, session_id: str) -> GatewaySession | None:
        self._expire_sessions()
        return self.sessions.get(session_id)

    def _expire_sessions(self) -> None:
        cutoff = time.time() - SESSION_KEEP_SECONDS
        for sid in [sid for sid, s in self.sessions.items()
                    if s.queue is None and s.detached_at and s.detached_at < cutoff]:
            self.sessions.pop(sid, None)

    # ---------------- 下发 ----------------

    def publish(self, event_type: str, data, event_id: str = "") -> None:
        if not self.active:
            return
        for session in self.sessions.values():
            if session.wants(event_type):
                session.push(event_type, data, event_id)
        urls = self.webhooks
        if urls:
            body = json.dumps({"op": 0, "id": event_id, "t": event_type, "d": data},
                              ensure_ascii=False).encode()
            for url in urls:
                task = asyncio.create_task(self._post(url, body))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _post(self, url: str, body: bytes) -> None:
        timestamp = str(int(time.time()))
        try:
            signature = _signing_key(self.bot.secret).sign(
                timestamp.encode() + body).signature.hex()
            import aiohttp  # noqa: PLC0415
            async with self.bot.http.post(
                url, data=body, timeout=aiohttp.ClientTimeout(total=WEBHOOK_TIMEOUT),
                headers={"Content-Type": "application/json",
                         "User-Agent": "QQBot-Callback",
                         "X-Bot-Appid": self.bot.appid,
                         "X-Signature-Ed25519": signature,
                         "X-Signature-Timestamp": timestamp},
            ) as resp:
                if resp.status >= 400:
                    logger.warning("[%s] 透传回调 %s 返回 HTTP %s",
                                   self.bot.appid, url, resp.status)
        except Exception as exc:
            logger.warning("[%s] 透传回调 %s 失败: %s", self.bot.appid, url, exc)

    def close(self) -> None:
        for session in self.sessions.values():
            session.kick()
        self.sessions.clear()
