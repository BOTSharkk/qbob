"""一键配置后端: 新 bot 按「后端预设」(data/provision.json)自动接上 OneBot 后端.

- onebot: 预设端点直接写成 bot 的 OneBot 端点; botshepherd: 在 BS 为每个 bot 建一条
  独立连接, 预设端点作其下游. 无 mode 的老配置按 botshepherd.
- 凭据不落盘: BS 用户名/端口现读 global_config.json, 密码只取环境变量或进程内存.
- 经 PUT /api/connections/<id> 热启连接; body 只带五个配置字段, 绝不回写 status.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from ..fileutil import atomic_write_private_text

logger = logging.getLogger("qqbot.provision")

MODES = ("onebot", "botshepherd")

DEFAULT_CONFIG = {
    "mode": "onebot",
    "bs": {
        "dir": "",               # botshepherd 模式必填
        "web_base": "",          # 留空由 web_port 推出
        "client_bind": "0.0.0.0",
        "port_start": 26070,
        "port_end": 26999,
    },
    "profiles": [],
}


class ProvisionError(Exception):
    status = 400                 # 管理台接口的 HTTP 状态


class BotNotFound(ProvisionError):
    status = 404


class AlreadyProvisioned(ProvisionError):
    """已有端点只许手改, 免得换掉在跑的连接."""
    status = 409


class NoDefaultProfile(ProvisionError):
    """未设默认预设, bot 保持未配置。"""

    status = 409


class ProvisionConfig:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {}
        self._runtime_password = ""   # 全局、仅内存, 不落盘
        self.load()

    def load(self) -> dict:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError as exc:
                logger.warning("provision.json 解析失败, 用默认值: %s", exc)
                self.data = json.loads(json.dumps(DEFAULT_CONFIG))
            self.data.setdefault("mode", "botshepherd")
        else:
            self.data = json.loads(json.dumps(DEFAULT_CONFIG))
            self.save()
        if self.data.get("mode") not in MODES:
            self.data["mode"] = "onebot"
        # 补齐缺失键
        for key, value in DEFAULT_CONFIG["bs"].items():
            self.data.setdefault("bs", {}).setdefault(key, value)
        self.data.setdefault("profiles", copy.deepcopy(DEFAULT_CONFIG["profiles"]))
        return self.data

    def save(self, new_data: dict | None = None) -> None:
        if new_data is not None:
            self.data = new_data
        atomic_write_private_text(
            self.path, json.dumps(self.data, ensure_ascii=False, indent=2))

    @property
    def mode(self) -> str:
        return self.data.get("mode", "onebot")

    def masked(self) -> dict:
        """管理台用的配置 + BS 现读信息; 含预设 access_token, 接口需 advanced."""
        data = json.loads(json.dumps(self.data))
        if not self.bs.get("dir"):
            data["_bs_runtime"] = {"error": "未配置 BotShepherd 根目录"}
            return data
        try:
            global_config = self.bs_global_config()
            data["_bs_runtime"] = {
                "username": global_config.get("web_auth", {}).get("username", ""),
                "web_port": global_config.get("web_port", 5100),
                "web_base": self.web_base(),
                "connections_dir": str(self.connections_dir()),
                "password_source": self.password_source(),
                "password_ready": self.password_source() != "none",
            }
        except Exception as exc:
            data["_bs_runtime"] = {"error": str(exc)}
        return data

    @property
    def bs(self) -> dict:
        return self.data["bs"]

    # ---- BS 侧的现读信息(不落盘) ----

    def bs_dir(self) -> Path:
        if not self.bs.get("dir"):
            raise ProvisionError("未配置 BotShepherd 根目录")
        return Path(self.bs["dir"])

    def connections_dir(self) -> Path:
        return self.bs_dir() / "config" / "connections"

    def bs_global_config(self) -> dict:
        path = self.bs_dir() / "config" / "global_config.json"
        if not path.is_file():
            raise ProvisionError(f"读不到 BotShepherd 配置: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def web_base(self) -> str:
        configured = (self.bs.get("web_base") or "").strip()
        if configured:
            return configured.rstrip("/")
        port = self.bs_global_config().get("web_port", 5100)
        return f"http://127.0.0.1:{port}"

    def set_runtime_password(self, password: str) -> None:
        """BS 密码全局一份, 仅存内存."""
        self._runtime_password = password

    def password_source(self) -> str:
        if self._runtime_password:
            return "session"
        if os.environ.get("BS_WEB_PASSWORD"):
            return "env"
        return "none"

    def bs_credentials(self, password: str = "") -> tuple[str, str]:
        """(用户名, 密码); 密码优先级: 入参 > 进程内存 > BS_WEB_PASSWORD."""
        username = self.bs_global_config().get("web_auth", {}).get("username", "")
        password = (password or self._runtime_password
                    or os.environ.get("BS_WEB_PASSWORD", ""))
        return username, password

    def profile(self, name: str = "") -> dict:
        profiles = self.data.get("profiles") or []
        if not profiles:
            raise ProvisionError("没有配置任何后端预设(profiles)")
        if name:
            for item in profiles:
                if item.get("name") == name:
                    return item
            raise ProvisionError(f"预设不存在: {name}")
        for item in profiles:
            if item.get("default"):
                return item
        raise NoDefaultProfile("没有设置默认后端预设")


def _connections(connections_dir: Path):
    """逐条产出 (连接 id, 监听端口), 读不了的跳过."""
    if not connections_dir.is_dir():
        return
    for path in sorted(connections_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        port = _port_of(str(data.get("client_endpoint", "")))
        if port:
            yield path.stem, port


def _ports_declared_in(connections_dir: Path) -> set[int]:
    return {port for _, port in _connections(connections_dir)}


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def pick_port(config: ProvisionConfig, reserved: set[int] | None = None) -> int:
    """挑空闲端口: 跳过已声明、已监听及 reserved(已分配未落盘)的."""
    bs = config.bs
    declared = _ports_declared_in(config.connections_dir()) | (reserved or set())
    for port in range(int(bs["port_start"]), int(bs["port_end"]) + 1):
        if port not in declared and _port_free(port):
            return port
    raise ProvisionError(
        f"{bs['port_start']}-{bs['port_end']} 区间内没有空闲端口")


@asynccontextmanager
async def bs_session(config: ProvisionConfig, password: str = ""):
    """登录 BS 面板, 产出 (会话, 地址) 或 None(无凭据); 连不上抛 aiohttp/OS 错误."""
    username, password = config.bs_credentials(password)
    if not (username and password):
        yield None
        return
    base = config.web_base()
    jar = aiohttp.CookieJar(unsafe=True)  # 目标是 127.0.0.1
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.post(
            f"{base}/login", data={"username": username, "password": password},
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status not in (301, 302, 303, 307, 308):
                raise ProvisionError("BotShepherd 登录失败(用户名或密码错误)")
        yield session, base


async def push_to_bs(
    config: ProvisionConfig, connection_id: str, connection: dict,
    password: str = "",
) -> bool:
    """经 BS web API 建连接并热启; 无密码返回 False."""
    async with bs_session(config, password) as bs:
        if bs is None:
            return False
        session, base = bs
        async with session.put(
            f"{base}/api/connections/{connection_id}", json=connection,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise ProvisionError(f"BS 创建连接失败: HTTP {resp.status} {body[:200]}")
    return True


def write_connection_file(config: ProvisionConfig, connection_id: str,
                          connection: dict, overwrite: bool = False) -> Path:
    directory = config.connections_dir()
    if not directory.is_dir():
        raise ProvisionError(f"BS 连接目录不存在: {directory}")
    path = directory / f"{connection_id}.json"
    if path.exists() and not overwrite:
        raise ProvisionError(f"连接配置已存在, 不覆盖: {path.name}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(connection, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def find_connection_by_endpoint(config: ProvisionConfig, ws_url: str) -> str | None:
    """按端点端口反查 BS 连接 id(id 可能被手改, 以 client_endpoint 为准)."""
    port = _port_of(ws_url)
    if not port:
        return None
    return next((cid for cid, p in _connections(config.connections_dir()) if p == port),
                None)


def _port_of(url: str) -> int:
    try:
        return urlparse(url or "").port or 0
    except ValueError:
        return 0


def owner_note(description: str) -> str:
    """规范成「号主 xxx」."""
    text = str(description or "").strip()
    if text and not text.startswith("号主"):
        text = f"号主 {text}"
    return text


async def delete_from_bs(config: ProvisionConfig, connection_id: str,
                         password: str = "") -> bool:
    """经 BS web API 删连接(并停止); 无密码返回 False."""
    async with bs_session(config, password) as bs:
        if bs is None:
            return False
        session, base = bs
        async with session.delete(
            f"{base}/api/connections/{connection_id}",
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            body = await resp.text()
            if resp.status not in (200, 204, 404):
                raise ProvisionError(
                    f"BS 删除连接失败: HTTP {resp.status} {body[:200]}")
    return True


def remove_connection_file(config: ProvisionConfig, connection_id: str) -> bool:
    """兜底: 直接删连接配置文件."""
    path = config.connections_dir() / f"{connection_id}.json"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProvisionError(f"删除连接配置失败: {exc}") from exc


def build_connection(config: ProvisionConfig, port: int, label: str,
                     description: str, profile_name: str = "") -> dict:
    """拼 BS 连接配置: name 为 bot QQ 号(缺则 appid), description 为号主; 不带 status."""
    profile = config.profile(profile_name)
    bind = config.bs.get("client_bind", "0.0.0.0")
    description = owner_note(description)
    return {
        "name": label,
        "description": description,
        "client_endpoint": f"ws://{bind}:{port}",
        "target_endpoints": list(profile["targets"]),
        "enabled": True,
    }


def direct_endpoints(config: ProvisionConfig, profile_name: str = "") -> list[dict]:
    """onebot 模式: 预设端点即 bot 端点."""
    profile = config.profile(profile_name)
    token = str(profile.get("access_token") or "")
    return [{"url": str(url), "access_token": token}
            for url in profile.get("targets") or [] if str(url).strip()]


async def retarget(manager, appid: str, profile_name: str = "") -> dict:
    """BS 模式: 把 bot 已有连接的下游换成预设的地址; 返回 {connection_id, targets, applied}."""
    cfg = manager.provision
    cfg.load()
    if cfg.mode != "botshepherd":
        raise ProvisionError("仅 BotShepherd 模式可用; 直连模式在端点里直接改")
    row = await manager.db.fetchone(
        "SELECT onebot_endpoints FROM bots WHERE appid=?", (appid,))
    if row is None:
        raise BotNotFound("bot 不存在")
    endpoints = json.loads(row["onebot_endpoints"] or "[]")
    connection_id = next(
        (cid for ep in endpoints
         if (cid := find_connection_by_endpoint(cfg, str(ep.get("url") or "")))), None)
    if connection_id is None:
        raise ProvisionError("没找到这个 bot 对应的 BotShepherd 连接; 没配过后端的请用「配置后端」")
    path = cfg.connections_dir() / f"{connection_id}.json"
    try:
        connection = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ProvisionError(f"读不了连接配置 {path.name}: {exc}") from exc
    connection["target_endpoints"] = list(cfg.profile(profile_name)["targets"])
    try:
        applied = await push_to_bs(cfg, connection_id, connection)
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
        logger.warning("连不上 BotShepherd, 改写配置文件: %s", exc)
        applied = False
    if not applied:
        write_connection_file(cfg, connection_id, connection, overwrite=True)
    return {"connection_id": connection_id, "applied": applied,
            "targets": connection["target_endpoints"]}


async def apply_profile(manager, appid: str, description: str,
                        profile_name: str = "", grp: str = "") -> dict:
    """给尚无端点的 bot 按当前模式配后端; 返回 {mode, endpoints, applied, port, connection_id, targets}."""
    cfg = manager.provision
    cfg.load()
    row = await manager.db.fetchone(
        "SELECT appid, bot_qq, onebot_endpoints, notes FROM bots WHERE appid=?",
        (appid,))
    if row is None:
        raise BotNotFound("bot 不存在, 请先添加")
    existing = json.loads(row["onebot_endpoints"] or "[]")
    if existing:
        raise AlreadyProvisioned(
            f"该 bot 已配置 {len(existing)} 个端点。已配置过的 bot 只允许在「编辑」里"
            "手动改端点，以免把正在跑的连接换掉。")

    result: dict = {"mode": cfg.mode, "applied": True, "port": 0,
                    "connection_id": "", "targets": []}
    if cfg.mode == "onebot":
        endpoints = direct_endpoints(cfg, profile_name)
        if not endpoints:
            raise ProvisionError("默认预设没有端点")
        result["targets"] = [e["url"] for e in endpoints]
        await manager.db.execute(
            "UPDATE bots SET onebot_endpoints=? WHERE appid=?",
            (json.dumps(endpoints, ensure_ascii=False), appid))
        note = owner_note(description)
        if note and not (row["notes"] or "").strip():
            await manager.db.execute(
                "UPDATE bots SET notes=? WHERE appid=?", (note, appid))
    else:
        live = manager.get_bot(appid)
        bot_qq = int((live.cfg.get("bot_qq") if live else row["bot_qq"]) or 0)
        label = str(bot_qq) if bot_qq else appid
        # 串行, 防并发挑到同一端口
        async with manager.provision_lock:
            port = 0
            try:
                # 逐个 bind 试探, 放线程里跑
                port = await asyncio.to_thread(
                    pick_port, cfg, set(manager.reserved_ports))
                manager.reserved_ports.add(port)
                connection = build_connection(cfg, port, label, description,
                                              profile_name)
                try:
                    applied = await push_to_bs(cfg, str(port), connection)
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                    # 连不上 BS: 退回写文件, 等 BS 启动时加载
                    logger.warning("连不上 BotShepherd, 改写配置文件: %s", exc)
                    applied = False
                if not applied:
                    write_connection_file(cfg, str(port), connection)
            except ProvisionError:
                manager.reserved_ports.discard(port)
                raise
        endpoints = [{"url": f"ws://127.0.0.1:{port}", "access_token": ""}]
        result.update(applied=applied, port=port, connection_id=str(port),
                      targets=connection["target_endpoints"])
        await manager.db.execute(
            "UPDATE bots SET onebot_endpoints=? WHERE appid=?",
            (json.dumps(endpoints, ensure_ascii=False), appid))
    result["endpoints"] = endpoints
    if grp:                                  # 成功才改分组
        from .groups import ensure  # noqa: PLC0415
        await manager.db.execute("UPDATE bots SET grp=? WHERE appid=?",
                                 (ensure(manager.config, grp), appid))
    await manager.reload_bot(appid)
    return result
