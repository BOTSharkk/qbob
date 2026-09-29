"""QQ 官方 webhook 事件接入.

- 路由: POST /qqbot/webhook 与 /qqbot/webhook/{appid}; bot 识别优先级:
  路径参数 > X-Bot-Appid 头.
- 每个事件带 Ed25519 签名(X-Signature-Ed25519 / X-Signature-Timestamp),
  密钥种子 = bot secret 重复拼接至 32 字节; 验签消息 = timestamp + body.
- op=13 回调地址校验: 用同一私钥对 event_ts + plain_token 签名回包.
- op=0 事件: 立即 ACK {op:12}, 异步进 bot 事件管线.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey

if TYPE_CHECKING:
    from ..core.manager import BotManager

logger = logging.getLogger("qqbot.webhook")


def _signing_key(secret: str) -> SigningKey:
    if not secret:
        raise ValueError("empty bot secret")
    seed = secret.encode()
    seed = seed * (32 // len(seed) + 1)
    return SigningKey(seed[:32])


def verify_signature(secret: str, timestamp: str, body: bytes, signature_hex: str) -> bool:
    try:
        key = _signing_key(secret)
        key.verify_key.verify(
            timestamp.encode() + body, bytes.fromhex(signature_hex)
        )
        return True
    except (BadSignatureError, ValueError):
        return False


def sign_validation(secret: str, event_ts: str, plain_token: str) -> str:
    key = _signing_key(secret)
    signed = key.sign((event_ts + plain_token).encode())
    return signed.signature.hex()


def build_router(manager: "BotManager") -> APIRouter:
    router = APIRouter()

    @router.post("/qqbot/webhook")
    @router.post("/qqbot/webhook/{appid}")
    async def webhook(request: Request, appid: str = "") -> Response:
        body = await request.body()
        bot_appid = appid or request.headers.get("X-Bot-Appid", "")
        client = request.client.host if request.client else "?"
        logger.info("webhook hit appid=%s from=%s ua=%r len=%d",
                    bot_appid, client, request.headers.get("user-agent", ""), len(body))
        bot = manager.get_bot(bot_appid)
        if bot is None:
            logger.warning("webhook for unknown/stopped appid=%r", bot_appid)
            return Response(status_code=403)

        signature = request.headers.get("X-Signature-Ed25519", "")
        timestamp = request.headers.get("X-Signature-Timestamp", "")
        if not verify_signature(bot.secret, timestamp, body, signature):
            logger.warning("[%s] webhook signature verify failed"
                           " (sig=%s ts=%s) — 检查后台 AppSecret 是否与本地一致",
                           bot.appid, "有" if signature else "无",
                           timestamp or "无")
            return Response(status_code=401)

        try:
            payload = json.loads(body)
        except ValueError:
            return Response(status_code=400)
        op = payload.get("op")

        if op == 13:  # 回调地址校验
            data = payload.get("d") or {}
            plain_token = str(data.get("plain_token", ""))
            event_ts = str(data.get("event_ts", ""))
            try:
                signature = sign_validation(bot.secret, event_ts, plain_token)
            except ValueError:
                return Response(status_code=500)
            return Response(
                content=json.dumps({
                    "plain_token": plain_token,
                    "signature": signature,
                }),
                media_type="application/json",
            )

        if op == 0:
            event_type = str(payload.get("t", ""))
            event_data = payload.get("d") or {}
            asyncio.get_running_loop().create_task(
                bot.dispatch_qq_event(event_type, event_data,
                                      str(payload.get("id", "")))
            )
        return Response(content=json.dumps({"op": 12}), media_type="application/json")

    return router
