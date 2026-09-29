#!/usr/bin/env bash
# 手动启动: uv 按 uv.lock 就地准备 .venv(没有就建)再运行. 首次会下载依赖.
# --inexact: 不卸掉 .venv 里多装的开发工具(pytest 等)
cd "$(dirname "$0")"
exec uv run --frozen --inexact python -m qqbot_onebot --config data/config.json
