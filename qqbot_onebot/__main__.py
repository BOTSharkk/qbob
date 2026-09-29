"""入口: python -m qqbot_onebot [--config data/config.json]. 公网/管理/HTTP API 三面共享一个 BotManager."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
from pathlib import Path

import uvicorn

from .config import DEFAULT_CONFIG_PATH, ServerConfig
from .core.manager import BotManager
from .web.app import build_admin_app, build_public_app
from .web.auth import AuthManager
from .web.http_api import build_http_api

logger = logging.getLogger("qqbot.main")


class _Server(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # 双 server 共存, 信号自己管
        pass


async def amain(config: ServerConfig) -> None:
    manager = BotManager(config)
    await manager.start()
    auth = AuthManager(manager.db, Path(config.db_path).parent)
    await auth.ensure_initial_admin()

    public_app = build_public_app(manager, config)
    admin_app = build_admin_app(manager, auth, config)
    public_app.state.manager = manager
    if config.admin_on_public:
        # SPA 用相对路径, 挂在 /admin 下也能用
        public_app.mount("/admin", build_admin_app(manager, auth, config))

    servers = [
        # 信任本机反代的 X-Forwarded-*: 重定向保持 https+原域名, 登录锁定拿到真实 IP
        _Server(uvicorn.Config(public_app, host=config.host, port=config.port,
                               log_level="warning", proxy_headers=True,
                               forwarded_allow_ips="127.0.0.1")),
        _Server(uvicorn.Config(admin_app, host=config.admin_host,
                               port=config.admin_port, log_level="warning")),
        _Server(uvicorn.Config(
            build_http_api(manager, config.http_api_token),
            host=config.http_api_host, port=config.http_api_port,
            log_level="warning")),
    ]
    logger.info("public %s:%s | admin %s:%s | http-api %s:%s",
                config.host, config.port, config.admin_host, config.admin_port,
                config.http_api_host, config.http_api_port)
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        await manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="qqbot-onebot")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args()

    config = ServerConfig.load(args.config)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO), format=fmt)
    if config.log_file:
        log_path = Path(config.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=max(1, config.log_max_mb) * 1024 * 1024,
            backupCount=max(0, config.log_backups), encoding="utf-8")
        handler.setFormatter(logging.Formatter(fmt))
        logging.getLogger().addHandler(handler)
    try:
        asyncio.run(amain(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
