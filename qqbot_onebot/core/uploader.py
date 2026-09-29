"""富媒体分片直传: upload_prepare -> PUT 分片到 COS -> upload_part_finish -> merge 得 file_info.

不需公网图床, 大文件不怕平台下载超时. file_info ttl=86400s 且跨群通用, 按内容哈希缓存,
广播多群只传一次. 外链仍可走 URL 上传兜底.

大小限制: 软限 图片 20MB / 视频 30MB / 语音 20MB / 文件 200MB, 硬限均 200MB.
超软限会被降级为「文件」发送, 故先尝试压缩保住类型; 超硬限压不下就报错.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from ..qq.api import QQApiError
from . import cdnkeys

logger = logging.getLogger("qqbot.uploader")

FILE_TYPE_IMAGE, FILE_TYPE_VIDEO, FILE_TYPE_VOICE, FILE_TYPE_FILE = 1, 2, 3, 4

MB = 1024 * 1024
HARD_LIMIT = 200 * MB
SOFT_LIMITS = {
    FILE_TYPE_IMAGE: 20 * MB,
    FILE_TYPE_VIDEO: 30 * MB,
    FILE_TYPE_VOICE: 20 * MB,
    FILE_TYPE_FILE: 200 * MB,
}
TYPE_NAMES = {FILE_TYPE_IMAGE: "图片", FILE_TYPE_VIDEO: "视频",
              FILE_TYPE_VOICE: "语音", FILE_TYPE_FILE: "文件"}

# md5_10m 取的前缀字节数(平台定值)
MD5_10M_BYTES = 10002432
# file_info 缓存提前失效的余量(秒)
CACHE_SAFETY_SECONDS = 300

# 可原地重试的错误码(抖动/超时/限频), 仍失败才回落 URL 上传
RETRYABLE_CODES = {40093001, 850027, 40034100, 40034106, 40054003, 11244, 22009}
UPLOAD_RETRIES = 2


class MediaTooLarge(Exception):
    """超过硬限制且压不下去, 报给插件."""


class MediaUploader:
    def __init__(self, bot):
        self.bot = bot
        self._locks: dict[str, asyncio.Lock] = {}
        # 上行流量计量(直传耗带宽, 命中缓存不耗)
        self.bytes_uploaded = 0
        self.bytes_saved = 0          # 命中缓存省下的上行
        self.uploads_done = 0
        self.cache_hits = 0

    # ---------------- file_info 缓存 ----------------

    def _scene(self, chat_type: str) -> str:
        """群上传的文件只能发到群聊, 两个场景分开缓存."""
        return "group" if chat_type == "group" else "private"

    async def _cached(self, digest: str, file_type: int, scene: str) -> dict | None:
        row = await self.bot.db.fetchone(
            "SELECT file_info, file_uuid, raw_url, raw_until, expires_at FROM media_cache"
            " WHERE bot_appid=? AND digest=? AND file_type=? AND scene=?",
            (self.bot.appid, digest, file_type, scene),
        )
        if row is None:
            return None
        now = int(time.time())
        if row["expires_at"] <= now:
            return None
        # raw_url 只活 1 小时, 过期也原样给, 调用方按 raw_until 判断
        return {"file_info": row["file_info"], "file_uuid": row["file_uuid"] or "",
                "raw_url": row["raw_url"] or "",
                "raw_until": int(row["raw_until"] or 0), "file_type": file_type}

    async def _remember(self, digest: str, file_type: int, scene: str,
                        merged: dict) -> None:
        # ttl=0 表示长期可用, 仍限一天
        ttl = merged["ttl"]
        seconds = 86400 if ttl <= 0 else max(60, ttl - CACHE_SAFETY_SECONDS)
        await self.bot.db.execute(
            "INSERT INTO media_cache (bot_appid, digest, file_type, scene,"
            " file_info, file_uuid, raw_url, raw_until, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(bot_appid, digest, file_type, scene) DO UPDATE SET"
            " file_info=excluded.file_info, file_uuid=excluded.file_uuid,"
            " raw_url=excluded.raw_url, raw_until=excluded.raw_until,"
            " expires_at=excluded.expires_at",
            (self.bot.appid, digest, file_type, scene, merged["file_info"],
             merged["file_uuid"], merged["raw_url"], merged["raw_until"],
             int(time.time()) + seconds),
        )

    # ---------------- 大小预判断与压缩 ----------------

    @staticmethod
    def _run(cmd: list[str], timeout: int = 120) -> bool:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("转码失败 %s: %s", cmd[0], exc)
            return False
        if proc.returncode != 0:
            logger.warning("转码失败: %s", proc.stderr[-300:])
            return False
        return True

    def _compress(self, data: bytes, file_type: int, target: int) -> bytes | None:
        """把图片/视频压到 target 以内; 压不动返回 None(语音/文件不压)."""
        if file_type not in (FILE_TYPE_IMAGE, FILE_TYPE_VIDEO):
            return None
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.write_bytes(data)
            if file_type == FILE_TYPE_IMAGE:
                # 逐档降质量+限长边
                for scale, quality in ((2048, 4), (1600, 6), (1280, 8)):
                    out = Path(tmp) / f"o{scale}.jpg"
                    ok = self._run([
                        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-vf", f"scale='min({scale},iw)':-2", "-q:v", str(quality),
                        str(out)])
                    if ok and out.exists() and out.stat().st_size <= target:
                        return out.read_bytes()
            else:
                for crf, height in ((28, 720), (32, 480)):
                    out = Path(tmp) / f"o{crf}.mp4"
                    ok = self._run([
                        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-vf", f"scale=-2:'min({height},ih)'",
                        "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
                        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
                        str(out)], timeout=600)
                    if ok and out.exists() and out.stat().st_size <= target:
                        return out.read_bytes()
        return None

    def _fit_limits(self, data: bytes, file_type: int) -> tuple[bytes, int]:
        """按限制预处理, 返回 (字节, 实际 file_type). 超硬限压不下抛 MediaTooLarge, 超软限压不下降级为文件."""
        name = TYPE_NAMES.get(file_type, "文件")
        size = len(data)
        if size > HARD_LIMIT:
            packed = self._compress(data, file_type, HARD_LIMIT)
            if packed is None:
                raise MediaTooLarge(
                    f"{name}大小 {size / MB:.1f}MB 超过平台硬限制 "
                    f"{HARD_LIMIT // MB}MB，且无法压缩到限制内")
            logger.info("[%s] %s 超硬限制, 压缩 %.1fMB -> %.1fMB",
                        self.bot.appid, name, size / MB, len(packed) / MB)
            data, size = packed, len(packed)

        soft = SOFT_LIMITS.get(file_type, HARD_LIMIT)
        if size > soft and file_type in (FILE_TYPE_IMAGE, FILE_TYPE_VIDEO):
            packed = self._compress(data, file_type, soft)
            if packed is not None:
                logger.info("[%s] %s 超软限制, 压缩 %.1fMB -> %.1fMB",
                            self.bot.appid, name, size / MB, len(packed) / MB)
                return packed, file_type
            # 平台会按文件发送, 类型跟着改
            logger.info("[%s] %s %.1fMB 压缩未达标, 降级为文件发送",
                        self.bot.appid, name, size / MB)
            return data, FILE_TYPE_FILE
        return data, file_type

    # ---------------- 直传 ----------------

    async def upload(self, chat_type: str, peer_openid: str, file_type: int,
                     data: bytes, file_name: str = "") -> str:
        """本地字节 -> file_info(命中缓存则零请求)."""
        done = await self.upload_media(chat_type, peer_openid, file_type, data, file_name)
        return done["file_info"]

    async def upload_media(self, chat_type: str, peer_openid: str, file_type: int,
                           data: bytes, file_name: str = "",
                           need_uuid: bool = False) -> dict:
        """-> {file_info, file_uuid, raw_url, raw_until, file_type}.

        file_uuid 即 CDN 直链的 fileid(见 cdnkeys); raw_url 为 COS 签名链接, 1 小时有效;
        file_type 为实际类型(可能降级). need_uuid: 缓存缺 file_uuid 时重传.
        """
        data, file_type = await asyncio.to_thread(
            self._fit_limits, data, file_type)
        digest = hashlib.sha256(data).hexdigest()
        scene = self._scene(chat_type)
        cached = await self._cached(digest, file_type, scene)
        if cached and (cached["file_uuid"] or not need_uuid):
            self.cache_hits += 1
            self.bytes_saved += len(data)
            logger.info("[%s] file_info 命中缓存 %s (%.2fMB 免上传, "
                        "累计省 %.1fMB / %d 次)", self.bot.appid, digest[:12],
                        len(data) / MB, self.bytes_saved / MB, self.cache_hits)
            return cached

        # 同一文件并发发多群只传一次
        key = f"{digest}:{file_type}:{scene}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                cached = await self._cached(digest, file_type, scene)
                if cached and (cached["file_uuid"] or not need_uuid):
                    return cached
                name = file_name or f"{digest[:16]}{_suffix_of(file_type)}"
                last: QQApiError | None = None
                for attempt in range(UPLOAD_RETRIES + 1):
                    try:
                        merged = await self._do_upload(
                            chat_type, peer_openid, file_type, data, name)
                    except QQApiError as exc:
                        last = exc
                        if (exc.code not in RETRYABLE_CODES
                                or attempt == UPLOAD_RETRIES):
                            raise
                        delay = 0.5 * (attempt + 1)
                        logger.warning("[%s] 直传失败(%s), %.1fs 后重试 %d/%d",
                                       self.bot.appid, exc.code, delay,
                                       attempt + 1, UPLOAD_RETRIES)
                        await asyncio.sleep(delay)
                        continue
                    await self._remember(digest, file_type, scene, merged)
                    self.bytes_uploaded += len(data)
                    self.uploads_done += 1
                    logger.info("[%s] 直传 %s %.2fMB -> file_info(ttl 缓存), "
                                "今日累计上行 %.1fMB / %d 次",
                                self.bot.appid, TYPE_NAMES.get(file_type, "文件"),
                                len(data) / MB, self.bytes_uploaded / MB,
                                self.uploads_done)
                    return {"file_info": merged["file_info"],
                            "file_uuid": merged["file_uuid"],
                            "raw_url": merged["raw_url"],
                            "raw_until": merged["raw_until"], "file_type": file_type}
                raise last or QQApiError(500, -1, "上传失败")
        finally:
            # 用完即弃: 键是内容哈希, 不清理会无限增长
            if not lock.locked() and not lock._waiters:
                self._locks.pop(key, None)

    async def _do_upload(self, chat_type: str, peer_openid: str, file_type: int,
                         data: bytes, file_name: str) -> dict:
        api = self.bot.api
        prep = await api.upload_prepare(
            chat_type, peer_openid, file_type, len(data), file_name,
            md5=hashlib.md5(data).hexdigest(),
            sha1=hashlib.sha1(data).hexdigest(),
            md5_10m=hashlib.md5(data[:MD5_10M_BYTES]).hexdigest(),
        )
        parts = sorted(prep.get("parts") or [], key=lambda p: p["index"])
        if not parts:
            raise QQApiError(500, -1, "upload_prepare 未返回分片")
        block = int(prep.get("block_size") or len(data))
        # 文档说 index 从 0 起, 实际从 1 起, 以最小值为基准
        base = parts[0]["index"]
        concurrency = max(1, int(
            (prep.get("upload_config") or {}).get("concurrency") or 1))
        sem = asyncio.Semaphore(concurrency)

        async def one(part: dict) -> None:
            index = part["index"] - base
            chunk = data[index * block:(index + 1) * block]
            async with sem:
                async with self.bot.http.put(
                    part["presigned_url"], data=chunk
                ) as resp:
                    if resp.status not in (200, 201, 204):
                        raise QQApiError(resp.status, -1,
                                         f"分片 {part['index']} 上传失败")
                await api.upload_part_finish(
                    chat_type, peer_openid, prep["upload_id"], part["index"],
                    len(chunk), hashlib.md5(chunk).hexdigest())

        await asyncio.gather(*(one(p) for p in parts))
        merged = await api.upload_merge(
            chat_type, peer_openid, file_type, prep["upload_id"], file_name)
        file_info = str(merged.get("file_info") or "")
        if not file_info:
            raise QQApiError(500, -1, "合并未返回 file_info")
        raw_url = str(merged.get("raw_url") or "")
        return {"file_info": file_info, "ttl": int(merged.get("ttl") or 0),
                "file_uuid": str(merged.get("file_uuid") or ""),
                "raw_url": raw_url,
                "raw_until": cdnkeys.raw_url_until(raw_url) if raw_url else 0}


def _suffix_of(file_type: int) -> str:
    return {FILE_TYPE_IMAGE: ".png", FILE_TYPE_VIDEO: ".mp4",
            FILE_TYPE_VOICE: ".silk"}.get(file_type, ".bin")
