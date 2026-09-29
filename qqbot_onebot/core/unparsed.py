"""未能完全解析的平台 payload 原样落盘(官方文档不全), 供事后补解析; 超限轮转."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("qqbot.unparsed")

MAX_BYTES = 8 * 1024 * 1024
FILE_NAME = "unparsed.jsonl"


def files(directory: str | Path) -> list[Path]:
    """当前文件与轮转的那一份."""
    base = Path(directory) / FILE_NAME
    return [base, base.with_suffix(".jsonl.1")]
MAX_VALUE_CHARS = 2000


def _truncate(value):
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE_CHARS else (
            value[:MAX_VALUE_CHARS] + f"…<+{len(value) - MAX_VALUE_CHARS}字符>"
        )
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v) for v in value[:50]]
    return value


class UnparsedLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, note: str, payload, appid: str = "") -> None:
        """kind: 形态标识; note: 记录原因; payload: 原始数据."""
        entry = {
            "ts": int(time.time()),
            "appid": appid,
            "kind": kind,
            "note": note,
            "payload": _truncate(payload),
        }
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                self.path.replace(self.path.with_suffix(".jsonl.1"))
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("写 unparsed 日志失败: %s", exc)
        logger.info("[%s] 未解析 %s: %s", appid or "-", kind, note)
