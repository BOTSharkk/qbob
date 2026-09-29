"""更新检查: 比对 GitHub main 分支的 __version__; 更新 = git pull --ff-only (+ uv sync).

update_mirror 是加在 GitHub 地址前的前缀, 取版本号与 pull 都走它。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path

import aiohttp

from .. import __version__
from ..config import PROJECT_ROOT

logger = logging.getLogger("qqbot.updater")

_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')
FIRST_CHECK_DELAY = 60
BRANCH = "main"
RUN_TIMEOUT = 180


def version_tuple(text: str) -> tuple:
    parts = []
    for piece in re.split(r"[.\-+]", text.strip().lstrip("v")):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def _mirrored(config, url: str) -> str:
    """加镜像前缀; 只认 https(它决定从哪拉代码)."""
    mirror = str(getattr(config, "update_mirror", "") or "").strip()
    if not mirror.startswith("https://"):
        if mirror:
            logger.warning("update_mirror 不是 https 地址, 已忽略: %s", mirror)
        return url
    return mirror.rstrip("/") + "/" + url


class Updater:
    def __init__(self, manager):
        self.manager = manager
        self.latest = ""
        self.checked_at = 0
        self.error = ""
        self.pulling = False
        self._task: asyncio.Task | None = None

    @property
    def config(self):
        return self.manager.config

    def snapshot(self) -> dict:
        has_update = bool(self.latest) and \
            version_tuple(self.latest) > version_tuple(__version__)
        return {"current": __version__, "latest": self.latest,
                "has_update": has_update, "checked_at": self.checked_at,
                "error": self.error, "repo": self.config.update_repo,
                "mirror": self.config.update_mirror}

    def start(self) -> None:
        if self.config.update_check_hours > 0 and self.config.update_repo:
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_CHECK_DELAY)
        while True:
            await self.check()
            await asyncio.sleep(max(1, self.config.update_check_hours) * 3600)

    async def check(self) -> dict:
        repo = self.config.update_repo
        url = _mirrored(self.config, f"https://raw.githubusercontent.com/{repo}/{BRANCH}"
                                     "/qqbot_onebot/__init__.py")
        try:
            async with self.manager.http.get(
                    url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
            match = _VERSION_RE.search(text)
            if not match:
                raise RuntimeError("没找到版本号")
            self.latest, self.error = match.group(1), ""
        except Exception as exc:        # noqa: BLE001
            self.error = f"检查更新失败: {exc or type(exc).__name__}"
            logger.info("%s (%s)", self.error, url)
        self.checked_at = int(time.time())
        return self.snapshot()

    async def pull(self) -> str:
        if self.pulling:
            raise RuntimeError("正在更新")
        if not (PROJECT_ROOT / ".git").exists():
            raise RuntimeError("项目目录不是 git 仓库, 请手动更新")
        self.pulling = True
        try:
            branch = await self._git("rev-parse", "--abbrev-ref", "HEAD")
            if branch != BRANCH:
                raise RuntimeError(f"当前在 {branch} 分支, 只在 {BRANCH} 上自动更新, 请手动处理")
            before = await self._git("rev-parse", "HEAD")
            args = ["pull", "--ff-only"]            # 无镜像走本地上游
            if _mirrored(self.config, "x") != "x":
                args += [_mirrored(self.config,
                                   f"https://github.com/{self.config.update_repo}.git"), BRANCH]
            await self._git(*args)
            after = await self._git("rev-parse", "HEAD")
            if before == after:
                return "已经是最新代码"
            changed = await self._git("diff", "--name-only", before, after)
            note = ""
            if "uv.lock" in changed.split():
                # systemd 的 PATH 常缺 ~/.local/bin
                uv = shutil.which("uv") or next(
                    (str(p) for p in [Path.home() / ".local" / "bin" / "uv"] if p.exists()), "")
                if uv:
                    await self._run(uv, "sync", "--frozen", "--inexact")
                    note = ", 依赖已同步"
                else:
                    note = ", 依赖有变化但找不到 uv, 请手动 uv sync"
            logger.info("已更新 %s -> %s%s", before[:8], after[:8], note)
            return f"已更新到 {after[:8]}{note}, 重启服务后生效"
        finally:
            self.pulling = False

    async def _git(self, *args: str) -> str:
        return await self._run("git", *args)

    async def _run(self, *cmd: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=PROJECT_ROOT, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()                 # 防残留 .git/index.lock
            await proc.wait()
            raise RuntimeError(f"{' '.join(cmd[:2])} 超时({RUN_TIMEOUT}s)") from None
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:2])} 失败: "
                               f"{(err or out).decode(errors='replace').strip()[-300:]}")
        return out.decode(errors="replace").strip()
