#!/usr/bin/env python3
"""从 NapCat 的 face_config.json(官方客户端自带)重新生成 onebot/faces.py.

刻意不做运行时拉取, 需要时手动跑; 人工挑的 NAME_EMOJI 原样保留。
    python scripts/update_faces.py [--check]   # --check: 有更新时以 1 退出
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

SOURCE = ("https://raw.githubusercontent.com/NapNeko/NapCatQQ/main/"
          "packages/napcat-core/external/face_config.json")
TARGET = Path(__file__).resolve().parent.parent / "qqbot_onebot/onebot/faces.py"

HEADER = '''"""QQ 表情 id 对照表(自动生成, 勿手改数据部分).

数据取自 NapCat 的 face_config.json(官方客户端自带):
- sysface: QSid = 系统表情 id, QDes = 名字(如 /赞), 无 unicode 对应;
- emoji:   QCid = unicode 码点十进制, QSid = 字符.
NAME_EMOJI 是人工挑的 sysface 视觉替身, 挑不出就显示 [名字].

更新: python scripts/update_faces.py (NAME_EMOJI 会原样保留)
"""
'''


def fetch() -> dict:
    with urllib.request.urlopen(SOURCE, timeout=30) as resp:  # noqa: S310 固定域名
        return json.loads(resp.read().decode("utf-8"))


def block(name: str, mapping: dict, comment: str) -> str:
    lines = [f"# {comment}", f"{name} = {{"]
    row = "   "
    for key, value in mapping.items():
        piece = f' "{key}": "{value}",'
        if len(row) + len(piece) > 86:
            lines.append(row)
            row = "   "
        row += piece
    if row.strip():
        lines.append(row)
    lines.append("}")
    return "\n".join(lines)


def current_name_emoji() -> dict:
    """读回人工挑的 NAME_EMOJI."""
    sys.path.insert(0, str(TARGET.parent.parent.parent))
    from qqbot_onebot.onebot.faces import NAME_EMOJI  # noqa: PLC0415
    return dict(NAME_EMOJI)


def render(data: dict, name_emoji: dict) -> str:
    sysface = {e["QSid"]: e["QDes"].lstrip("/")
               for e in data["sysface"] if e.get("QSid")}
    emoji = {e["QCid"]: e["QSid"]
             for e in data["emoji"] if e.get("QCid") and e.get("QSid")}
    # 清掉官方表里已没有的名字
    kept = {k: v for k, v in name_emoji.items() if k in set(sysface.values())}
    dropped = sorted(set(name_emoji) - set(kept))
    if dropped:
        print(f"提示: 这些名字已不在官方表里, 已从 NAME_EMOJI 移除: {dropped}")
    return (HEADER
            + block("SYSFACE_NAMES", sysface, "QQ 系统表情 id -> 名字(去掉前导 /)")
            + "\n\n"
            + block("EMOJI_BY_CODE", emoji, "unicode 码点(十进制) -> 字符")
            + "\n\n"
            + block("NAME_EMOJI", kept,
                    "系统表情名 -> 视觉替身(人工挑, 挑不出就显示 [名字])")
            + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="只比对, 有更新时以 1 退出(给 CI 用)")
    args = parser.parse_args()

    fresh = render(fetch(), current_name_emoji())
    old = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    if fresh == old:
        print("表已是最新")
        return 0
    if args.check:
        print("官方表有更新, 跑一次 python scripts/update_faces.py")
        return 1
    TARGET.write_text(fresh, encoding="utf-8")
    print(f"已更新 {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
