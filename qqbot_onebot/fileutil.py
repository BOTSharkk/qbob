"""少量跨模块的安全文件操作。"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def atomic_write_private_text(path: str | Path, text: str) -> None:
    """以 0600 原子写文本，失败时不替换原文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = -1
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            fd = -1
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
