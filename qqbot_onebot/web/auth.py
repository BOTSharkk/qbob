"""管理前端认证: 会话 cookie + 三级角色(admin > advanced > user).

密码 pbkdf2_hmac-sha256; 首次启动无用户时自动建 admin, 随机密码写入
data/initial_admin_password.txt 并打日志.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from pathlib import Path

from fastapi import Depends, HTTPException, Request

from ..db import Database
from ..fileutil import atomic_write_private_text

logger = logging.getLogger("qqbot.auth")

SESSION_COOKIE = "qqob_session"
SESSION_TTL = 7 * 86400
ROLE_LEVEL = {"user": 1, "advanced": 2, "admin": 3}


def hash_password(password: str, salt: str | None = None, iterations: int = 200_000) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations
    ).hex()
    return f"pbkdf2${iterations}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(iterations)
        ).hex()
        return secrets.compare_digest(candidate, digest)
    except ValueError:
        return False


MAX_LOGIN_FAILURES = 5
LOGIN_FAILURE_TTL = 3600      # 失败记录保留时长
LOCKOUT_BASE_SECONDS = 30
MAX_IP_FAILURES = 20
MAX_FAILURE_ENTRIES = 10_000
MAX_IP_ENTRIES = 10_000
LOOPBACK_IPS = {"127.0.0.1", "::1", ""}
PASSWORD_MIN, PASSWORD_MAX, USERNAME_MAX = 8, 1024, 128


def credential_error(username: str | None, password: str) -> str:
    """新设的用户名/密码不合规时返回原因, 合规返回空串."""
    if username is not None and not (1 <= len(username) <= USERNAME_MAX):
        return f"用户名需 1~{USERNAME_MAX} 个字符"
    if not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        return f"密码需 {PASSWORD_MIN}~{PASSWORD_MAX} 位"
    return ""

# 不存在的账号也校验这个预计算 hash, 抹平时序差异
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def cookie_values(request: Request, name: str) -> list[str]:
    """Cookie 头里 name 的全部取值; request.cookies 遇到同名只留最后一个."""
    out = []
    for part in request.headers.get("cookie", "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name and value:
            out.append(value)
    return out


class AuthManager:
    def __init__(self, db: Database, data_dir: Path):
        self.db = db
        self.data_dir = data_dir
        self._sessions: dict[str, dict] = {}
        # (ip, username) -> [failure_count, locked_until_ts]
        self._login_failures: dict[tuple[str, str], list[float]] = {}
        # 按 IP 的总闸, 防随机用户名绕过 (ip, username) 锁定
        self._ip_failures: dict[str, list[float]] = {}

    def _lockout_remaining(self, key: tuple[str, str]) -> float:
        entry = self._login_failures.get(key)
        if entry is None:
            return 0
        return max(0.0, entry[1] - time.time())

    def _sweep(self, now: float) -> None:
        """清掉过期失败记录与会话并封顶条数, 防随机用户名撑爆内存."""
        if len(self._login_failures) >= 1000:
            for key in [k for k, v in self._login_failures.items()
                        if now - v[2] > LOGIN_FAILURE_TTL and v[1] < now]:
                self._login_failures.pop(key, None)
        for ip in [k for k, v in self._ip_failures.items()
                   if now - v[2] > LOGIN_FAILURE_TTL and v[1] < now]:
            self._ip_failures.pop(ip, None)
        while len(self._login_failures) >= MAX_FAILURE_ENTRIES:
            self._login_failures.pop(next(iter(self._login_failures)))
        while len(self._ip_failures) >= MAX_IP_ENTRIES:
            self._ip_failures.pop(next(iter(self._ip_failures)))
        if len(self._sessions) >= 500:
            for token in [t for t, sess in self._sessions.items()
                          if sess["expires"] < now]:
                self._sessions.pop(token, None)

    def _record_failure(self, key: tuple[str, str]) -> None:
        now = time.time()
        self._sweep(now)
        entry = self._login_failures.setdefault(key, [0, 0.0, now])
        entry[0] += 1
        entry[2] = now
        if entry[0] >= MAX_LOGIN_FAILURES:
            # 指数退避: 5 次后 30s, 之后每次翻倍, 上限 1h
            excess = entry[0] - MAX_LOGIN_FAILURES
            entry[1] = now + min(LOCKOUT_BASE_SECONDS * (2 ** excess), 3600)
        if key[0] in LOOPBACK_IPS:
            # 回环不进 IP 总闸: 未经受信反代时人人都是 127.0.0.1, 锁它会连管理员一起锁
            return
        ip_entry = self._ip_failures.setdefault(key[0], [0, 0.0, now])
        ip_entry[0] += 1
        ip_entry[2] = now
        if ip_entry[0] >= MAX_IP_FAILURES:
            excess = ip_entry[0] - MAX_IP_FAILURES
            ip_entry[1] = now + min(LOCKOUT_BASE_SECONDS * (2 ** excess), 3600)

    async def ensure_initial_admin(self) -> None:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM web_users")
        if row and row["n"] > 0:
            return
        password = secrets.token_urlsafe(12)
        password_hash = hash_password(password)
        # 先落盘密码再建用户, 否则写文件失败会留下无人知道密码的 admin
        atomic_write_private_text(
            self.initial_password_file, f"admin / {password}\n")
        await self.db.execute(
            "INSERT INTO web_users (username, password_hash, role, created_at)"
            " VALUES (?,?,?,?)",
            ("admin", password_hash, "admin", int(time.time())),
        )
        logger.warning("已创建初始管理员 admin, 密码见 %s",
                       self.initial_password_file)

    async def login(
        self, username: str, password: str, client_ip: str = ""
    ) -> tuple[str, str] | None:
        if len(username) > USERNAME_MAX or len(password) > PASSWORD_MAX:
            return None
        key = (client_ip, username)
        ip_entry = self._ip_failures.get(client_ip)
        remaining = max(self._lockout_remaining(key),
                        max(0.0, ip_entry[1] - time.time()) if ip_entry else 0.0)
        if remaining > 0:
            logger.warning("login locked out: user=%s ip=%s (%.0fs left)",
                           username, client_ip, remaining)
            return None
        row = await self.db.fetchone(
            "SELECT * FROM web_users WHERE username=?", (username,)
        )
        # pbkdf2 走线程池; 用户不存在也跑假哈希抹平时序
        stored = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
        valid = await asyncio.to_thread(verify_password, password, stored)
        if row is None or not valid:
            self._record_failure(key)
            logger.warning("login failed: user=%s ip=%s", username, client_ip)
            return None
        self._login_failures.pop(key, None)
        self._ip_failures.pop(client_ip, None)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {
            "username": username,
            "role": row["role"],
            "expires": time.time() + SESSION_TTL,
        }
        return token, row["role"]

    def logout(self, token: str) -> None:
        self._sessions.pop(token, None)

    def session_token(self, request: Request) -> str:
        """当前有效会话的 token. 同名 cookie 可能有多个(不同 path 的旧 cookie), 逐个试."""
        for token in cookie_values(request, SESSION_COOKIE):
            session = self._sessions.get(token)
            if session is None:
                continue
            if session["expires"] < time.time():
                self._sessions.pop(token, None)
                continue
            return token
        return ""

    def session_of(self, request: Request) -> dict | None:
        return self._sessions.get(self.session_token(request))

    def drop_user_sessions(self, username: str, keep: str = "") -> None:
        for token in [t for t, s in self._sessions.items()
                      if s["username"] == username and t != keep]:
            self._sessions.pop(token, None)

    # ---- 初始密码 ----

    @property
    def initial_password_file(self) -> Path:
        return self.data_dir / "initial_admin_password.txt"

    async def check_password(self, username: str, password: str) -> bool:
        if len(username) > USERNAME_MAX or len(password) > PASSWORD_MAX:
            return False
        row = await self.db.fetchone(
            "SELECT password_hash FROM web_users WHERE username=?", (username,))
        return bool(row) and await asyncio.to_thread(
            verify_password, password, row["password_hash"])

    async def uses_initial_password(self, username: str) -> bool:
        """admin 是否仍在用首启随机密码(管理台据此引导改密)."""
        path = self.initial_password_file
        if username != "admin" or not path.exists():
            return False
        try:
            password = path.read_text(encoding="utf-8").strip().split(" / ", 1)[1]
        except (OSError, IndexError):
            return False
        return await self.check_password(username, password)

    async def set_password(self, username: str, password: str, keep: str = "") -> None:
        """改密码并踢掉除 keep 外的会话; admin 改密后删掉明文初始密码文件."""
        await self.db.execute(
            "UPDATE web_users SET password_hash=? WHERE username=?",
            (await asyncio.to_thread(hash_password, password), username))
        self.drop_user_sessions(username, keep=keep)
        if username == "admin":
            self.initial_password_file.unlink(missing_ok=True)

    def clear_initial_password(self, username: str) -> None:
        if username == "admin":
            self.initial_password_file.unlink(missing_ok=True)


def require_role(min_role: str):
    async def dependency(request: Request) -> dict:
        auth: AuthManager = request.app.state.auth
        session = auth.session_of(request)
        if session is None:
            raise HTTPException(status_code=401, detail="未登录")
        if ROLE_LEVEL.get(session["role"], 0) < ROLE_LEVEL[min_role]:
            raise HTTPException(status_code=403, detail="权限不足")
        return session

    return Depends(dependency)
