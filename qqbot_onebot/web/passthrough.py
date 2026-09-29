"""QQ 官方协议透传的 HTTP / WebSocket 入口(挂在 HTTP API 面上).

框架侧配置(以 nonebot-adapter-qq 为例):
    QQ_AUTH_BASE = "http://127.0.0.1:17820/qqapi/app/getAppAccessToken"
    QQ_API_BASE  = "http://127.0.0.1:17820/qqapi/"
id/secret 照填平台真实值. 协议细节见 core/passthrough.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from ..core import passthrough as pt
from ..core.manager import BotManager

logger = logging.getLogger("qqbot.passthrough")

IDENTIFY_TIMEOUT = 30
# 客户端心跳超时即断开(平台 heartbeat_interval 为 41.25s)
HEARTBEAT_GRACE = 120
MAX_PENDING_WS_PER_IP = 8
MAX_GATEWAY_WS = 256


def _error(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def build_router(manager: BotManager) -> APIRouter:
    router = APIRouter(prefix="/qqapi")
    ws_by_ip: dict[str, int] = {}
    ws_total = [0]

    def bot_by_auth(request: Request):
        appid = pt.appid_of_token(request.headers.get("authorization", ""),
                                  manager.bots.values())
        return manager.get_bot(appid) if appid else None

    @router.post("/app/getAppAccessToken")
    async def get_token(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return _error(400, 100007, "invalid body")
        appid = str(body.get("appId") or "")
        secret = str(body.get("clientSecret") or "")
        bot = manager.get_bot(appid)
        if bot is None or not secrets.compare_digest(secret, bot.secret):
            return _error(401, 100016, "invalid appid or secret")
        try:
            token = await bot.api.get_token()
        except Exception as exc:
            logger.warning("[%s] 透传取 token 失败: %s", appid, exc)
            return _error(502, 100000, "get token failed")
        expire_at = bot.api._token_expire_at
        pt.issue(appid, token, expire_at)
        return {"access_token": token,
                "expires_in": str(max(0, int(expire_at - time.time())))}

    def gateway_url(request: Request) -> str:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        scheme = "wss" if proto == "https" else "ws"
        host = request.headers.get("host") or f"{request.url.hostname}:{request.url.port}"
        return f"{scheme}://{host}/qqapi/ws"

    @router.get("/gateway")
    async def gateway(request: Request):
        if bot_by_auth(request) is None:
            return _error(401, 11241, "unauthorized")
        return {"url": gateway_url(request)}

    @router.get("/gateway/bot")
    async def gateway_bot(request: Request):
        if bot_by_auth(request) is None:
            return _error(401, 11241, "unauthorized")
        return {"url": gateway_url(request), "shards": 1,
                "session_start_limit": {"total": 1000, "remaining": 1000,
                                        "reset_after": 86400000,
                                        "max_concurrency": 1}}

    @router.websocket("/ws")
    async def gateway_ws(ws: WebSocket):
        client_ip = ws.client.host if ws.client else "?"
        if (ws_total[0] >= MAX_GATEWAY_WS
                or ws_by_ip.get(client_ip, 0) >= MAX_PENDING_WS_PER_IP):
            await ws.close(4429)
            return
        ws_total[0] += 1
        ws_by_ip[client_ip] = ws_by_ip.get(client_ip, 0) + 1
        released = [False]

        def release_slot() -> None:
            if released[0]:
                return
            released[0] = True
            ws_total[0] -= 1
            left = ws_by_ip.get(client_ip, 1) - 1
            if left:
                ws_by_ip[client_ip] = left
            else:
                ws_by_ip.pop(client_ip, None)

        try:
            await ws.accept()
            await ws.send_json({"op": 10, "d": {"heartbeat_interval":
                                               pt.HEARTBEAT_INTERVAL_MS}})
        except Exception:
            release_slot()
            return
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(),
                                                      IDENTIFY_TIMEOUT))
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            await ws.close(4009)
            release_slot()
            return
        data = first.get("d") if isinstance(first, dict) else None
        data = data if isinstance(data, dict) else {}
        appid = pt.appid_of_token(str(data.get("token") or ""), manager.bots.values())
        bot = manager.get_bot(appid) if appid else None
        if bot is None or first.get("op") not in (2, 6):
            await ws.send_json({"op": 9, "d": False})
            await ws.close(4004)
            release_slot()
            return
        # 已认证, 释放未认证握手名额
        release_slot()
        hub = bot.passthrough
        if first["op"] == 6:
            session = hub.find_session(str(data.get("session_id") or ""))
            if session is None:
                await ws.send_json({"op": 9, "d": False})
                await ws.close(4009)
                release_slot()
                return
            backlog = session.replay_after(int(data.get("seq") or 0))
        else:
            session = hub.open_session(int(data.get("intents") or 0))
            backlog = []
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
        session.kick()                      # 同一会话的旧连接让位
        session.queue = queue
        if first["op"] == 2:
            session.push("READY", {
                "version": 1, "session_id": session.session_id,
                "user": {"id": bot.appid, "username": bot.name, "bot": True},
                "shard": data.get("shard") or [0, 1]})
        else:
            for frame in backlog:
                queue.put_nowait(frame)
            session.push("RESUMED", "")
        logger.info("[%s] 透传网关%s: 会话 %s…", bot.appid,
                    "续上" if first["op"] == 6 else "接入", session.session_id[:8])

        last_beat = [time.monotonic()]

        async def reader() -> None:
            while True:
                frame = json.loads(await ws.receive_text())
                if isinstance(frame, dict) and frame.get("op") == 1:
                    last_beat[0] = time.monotonic()
                    await ws.send_json({"op": 11})

        async def writer() -> None:
            while True:
                frame = await queue.get()
                if not frame:
                    return
                await ws.send_text(frame)

        async def watchdog() -> None:
            while time.monotonic() - last_beat[0] < HEARTBEAT_GRACE:
                await asyncio.sleep(10)

        tasks = [asyncio.create_task(t()) for t in (reader, writer, watchdog)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            if session.queue is queue:
                session.queue = None
                session.detached_at = time.time()
            try:
                await ws.close()
            except Exception:
                pass
            release_slot()

    @router.api_route("/{path:path}",
                      methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request):
        bot = bot_by_auth(request)
        if bot is None:
            return _error(401, 11241, "unauthorized")
        path_qs = "/" + path
        if request.url.query:
            path_qs += "?" + request.url.query
        try:
            status, content_type, body = await bot.api.raw(
                request.method, path_qs, await request.body(),
                request.headers.get("content-type", ""))
        except Exception as exc:
            logger.warning("[%s] 透传 %s %s 失败: %s", bot.appid, request.method,
                           path, exc)
            return _error(502, 100000, "upstream unavailable")
        return Response(body, status_code=status,
                        media_type=content_type or "application/json")

    return router
