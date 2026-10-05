#!/usr/bin/env python3
"""qbob 容器入口: 启动时按平台环境变量重写 config.json 的端口, 再拉起 qbob。

为什么需要它:
  qbob 的端口只从 config.json 读取(ServerConfig.load 没有环境变量支持),
  而 Zeabur 这类 PaaS 只注入一个 $PORT 并要求服务监听它。
  本脚本在启动瞬间把 config.json 里的端口改写成平台分配的值。

容器 CMD:
  python scripts/zeabur-entrypoint.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("QBOB_CONFIG") or (ROOT / "data" / "config.json"))

# 平台注入的端口。Zeabur 用 PORT; 取不到时回退到 qbob 默认 17800。
PUBLIC_PORT = int(os.environ.get("PORT") or 17800)


def main() -> int:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        example = ROOT / "config.example.json"
        CONFIG.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[entrypoint] 已从模板创建 {CONFIG}", flush=True)

    data = json.loads(CONFIG.read_text(encoding="utf-8"))

    # 公网面必须监听平台端口, 且监听 0.0.0.0(容器外才能访问)。
    data["host"] = "0.0.0.0"
    data["port"] = PUBLIC_PORT
    # 管理台挂在公网面的 /admin 下, 这样不需要第二个公网端口。
    data["admin_on_public"] = True
    # 容器里没有本机反代, 三面都监听 0.0.0.0 更省事(平台只暴露一个端口)。
    # admin_port / http_api_port 保持配置原值, 不对外暴露即可。
    data.setdefault("log_file", "./data/logs/qqbot.log")
    data["db_path"] = "./data/qqbot_onebot.db"
    data["media_dir"] = "./data/media"

    # public_base_url 决定"合并转发转网页"和 webhook 回跳地址。
    # 平台通常会把外部域名放进环境变量, 取到就覆盖, 取不到保持配置文件原值。
    domain = (
        os.environ.get("ZEABUR_WEB_URL")
        or os.environ.get("PUBLIC_DOMAIN")
        or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        or ""
    ).strip()
    if domain:
        if not domain.startswith(("http://", "https://")):
            domain = "https://" + domain
        data["public_base_url"] = domain.rstrip("/")
        print(f"[entrypoint] public_base_url = {data['public_base_url']}", flush=True)

    CONFIG.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[entrypoint] 监听 {data['host']}:{data['port']} "
        f"(admin_on_public={data['admin_on_public']})",
        flush=True,
    )

    # 拉起 qbob 并转发信号, 让平台的停止/重启动作正常生效。
    cmd = [sys.executable, "-m", "qqbot_onebot", "--config", str(CONFIG)]
    proc = subprocess.Popen(cmd, cwd=str(ROOT))

    def forward(signum, _frame):
        proc.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())