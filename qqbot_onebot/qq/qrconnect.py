"""QQ 官方"扫码连接"接入(第三方 Agent 接入).

官方无需资质/鉴权的绑定流程(同 @tencent-connect/qqbot-connector,
文档 https://bot.q.qq.com/wiki/agent-qqbot/):

1. POST /lite/create_bind_task  {"key": base64(32 随机字节)} -> task_id
2. 二维码内容 = https://q.qq.com/qqbot/openclaw/connect.html?task_id=..&source=..&_wv=2
   机器人主人用手机 QQ 扫码并确认
3. 轮询 POST /lite/poll_bind_result {"task_id"} -> status/bot_appid/
   bot_encrypt_secret/user_openid
4. AppSecret 用第 1 步那把 key 做 AES-256-GCM 解密(iv 前 12 字节, tag 后 16 字节)

task_id 随二维码公开, 但只有持 key 的本进程能解出 secret.

status: 0 未开始 / 1 待确认 / 2 已完成 / 3 已过期
"""

from __future__ import annotations

import base64
import logging
import secrets
from dataclasses import dataclass, field

import aiohttp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("qqbot.qrconnect")

API_BASE = "https://q.qq.com"
CONNECT_PAGE = "https://q.qq.com/qqbot/openclaw/connect.html"
# source 只影响扫码页显示的接入方名称
DEFAULT_SOURCE = ""

STATUS_NONE, STATUS_PENDING, STATUS_COMPLETED, STATUS_EXPIRED = 0, 1, 2, 3


class QrConnectError(Exception):
    pass


@dataclass
class BindTask:
    task_id: str
    key_b64: str
    created_at: float
    source: str = ""
    result: dict = field(default_factory=dict)

    @property
    def connect_url(self) -> str:
        url = f"{CONNECT_PAGE}?task_id={self.task_id}"
        if self.source:
            url += f"&source={self.source}"
        return url + "&_wv=2"


async def create_bind_task(
    session: aiohttp.ClientSession, source: str = DEFAULT_SOURCE
) -> BindTask:
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    async with session.post(
        f"{API_BASE}/lite/create_bind_task", json={"key": key},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        data = await resp.json(content_type=None)
    if data.get("retcode") != 0 or not (data.get("data") or {}).get("task_id"):
        raise QrConnectError(f"创建绑定任务失败: {data}")
    import time  # noqa: PLC0415
    return BindTask(task_id=data["data"]["task_id"], key_b64=key,
                    created_at=time.time(), source=source)


async def poll_bind_result(
    session: aiohttp.ClientSession, task_id: str
) -> dict:
    async with session.post(
        f"{API_BASE}/lite/poll_bind_result", json={"task_id": task_id},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        data = await resp.json(content_type=None)
    if data.get("retcode") != 0:
        raise QrConnectError(f"轮询失败: {data}")
    return data.get("data") or {}


def decrypt_secret(key_b64: str, encrypted_b64: str) -> str:
    """AES-256-GCM: iv(12) || ciphertext || tag(16)."""
    payload = base64.b64decode(encrypted_b64)
    if len(payload) < 29:
        raise QrConnectError("密文长度异常")
    iv, tag, ciphertext = payload[:12], payload[-16:], payload[12:-16]
    try:
        plain = AESGCM(base64.b64decode(key_b64)).decrypt(iv, ciphertext + tag, None)
    except Exception as exc:
        raise QrConnectError(f"解密 AppSecret 失败: {exc}") from exc
    return plain.decode("utf-8")


def qr_svg(text: str, box_size: int = 8) -> str:
    """生成二维码 SVG(不依赖 Pillow), 直接内嵌前端."""
    import qrcode  # noqa: PLC0415
    import qrcode.image.svg  # noqa: PLC0415
    import io  # noqa: PLC0415

    qr = qrcode.QRCode(box_size=box_size, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")
