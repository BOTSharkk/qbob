#!/usr/bin/env python3
"""qbob 容器入口: 启动时按平台环境变量重写 config.json 的端口, 再拉起 qbob.

为什么需要它:
  qbob 的端口只从 config.json 读取(ServerConfig.load 没有环境变量支持),
  而 Zeabur 这类 PaaS 要求服务监听它指定的端口。
  本脚本在启动瞬间把 config.json 里的端口改写成平台分配的值。

端口解析优先级(高 -> 低):
  1. QBOB_PORT   显式覆盖, 最保险; 建议在 Zeabur 环境变量里设成对外端口
  2. PORT        Zeabur / Railway 等平台注入
  3. 7788        默认值, 与 Zeabur 面板里配置的对外端口保持一致

启动后会打印监听地址, 并在最多 15 秒内自检端口是否真的起来了, 便于定位问题。

容器 CMD:
  python scripts/zeabur-entrypoint.py
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("QBOB_CONFIG") or (ROOT / "data" / "config.json"))

#: 兜底端口。必须与 Zeabur 面板里配置的对外端口一致, 否则健康检查会 connection refused。
DEFAULT_PORT = 7788


def resolve_port() -> tuple[int, str]:
    """返回 (端口, 来源说明). 同时把原始环境变量值带出来便于排查。"""
    for key in ("QBOB_PORT", "PORT"):
        raw = (os.environ.get(key) or "").strip()
        if raw.isdigit() and 0 < int(raw) < 65536:
            return int(raw), f"{key}={raw}"
        if raw:
            print(f"[entrypoint] 忽略无效的 {key}={raw!r}", flush=True)
    return DEFAULT_PORT, f"默认值(未检测到 QBOB_PORT / PORT)"


def probe(host: str, port: int, timeout: float = 1.0) -> bool:
    """本机 TCP 连接测试: 判断端口是否真的在监听。"""
    target = "127.0.0.1" if host in ("", "0.0.0.0") else host
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    port, source = resolve_port()

    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        example = ROOT / "config.example.json"
        if not example.exists():
            print(f"[entrypoint] 致命: 找不到模板 {example}", flush=True)
            return 1
        CONFIG.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[entrypoint] 已从模板创建 {CONFIG}", flush=True)

    data = json.loads(CONFIG.read_text(encoding="utf-8"))

    # 公网面必须监听平台端口, 且监听 0.0.0.0(容器外才能访问)。
    data["host"] = "0.0.0.0"
    data["port"] = port
    # 管理台挂在公网面的 /admin 下, 这样不需要第二个公网端口。
    data["admin_on_public"] = True
    # 容器里没有本机反代, 其余两面保持配置原值即可(不对外暴露)。
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
        f"[entrypoint] 监听 {data['host']}:{data['port']} (来源: {source})",
        flush=True,
    )
    print(
        f"[entrypoint] 数据目录 {CONFIG.parent} | "
        f"可写={os.access(CONFIG.parent, os.W_OK)}",
        flush=True,
    )

    # 拉起 qbob 并转发信号, 让平台的停止/重启动作正常生效。
    cmd = [sys.executable, "-m", "qqbot_onebot", "--config", str(CONFIG)]
    print(f"[entrypoint] 启动: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, cwd=str(ROOT))

    def forward(signum, _frame):
        proc.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    # 自检: 最多等 15 秒, 确认端口真的在监听; 早退则直接报退出码。
    for _ in range(15):
        if proc.poll() is not None:
            print(
                f"[entrypoint] qbob 提前退出, exit code={proc.returncode}",
                flush=True,
            )
            return proc.returncode or 1
        if probe(data["host"], data["port"]):
            print(f"[entrypoint] 端口 {data['port']} 已监听, 服务就绪", flush=True)
            break
        time.sleep(1)
    else:
        print(
            f"[entrypoint] 警告: 15 秒内端口 {data['port']} 仍未监听。"
            f"健康检查会失败 —— 请核对 Zeabur 里的对外端口是否为 {data['port']}。",
            flush=True,
        )

    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
