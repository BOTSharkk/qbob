"""渲染 README 的仿 QQ 聊天示意图(docs/assets/demo_*.png), 需 google-chrome.

    uv run python scripts/render_demo.py
"""

from __future__ import annotations

import base64
import html
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "assets"
ICON = OUT / "icon.png"

BOT = "小Q"
BOT_ID = "100000000000001"
GROUP_ID = "100000000000002"
USER_ID = "100000000000003"
CODE = f"QQOB1|bot={BOT_ID}|group={GROUP_ID}"

CSS = """
* { box-sizing: border-box; margin: 0; }
body { font-family: "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  background: transparent; }
.phone { width: 390px; background: #f2f3f5; border-radius: 28px; overflow: hidden;
  border: 1px solid #dcdfe6; }
.bar { height: 56px; background: #f7f8fa; border-bottom: 1px solid #e5e6eb; display: flex;
  align-items: center; justify-content: center; position: relative; font-size: 17px;
  font-weight: 600; color: #1f2329; }
.bar .back { position: absolute; left: 16px; font-size: 22px; font-weight: 400; color: #1f2329; }
.bar .sub { font-size: 12px; font-weight: 400; color: #8a8f99; margin-left: 4px; }
.chat { padding: 14px 12px 18px; display: flex; flex-direction: column; gap: 14px; }
.time { align-self: center; font-size: 12px; color: #8a8f99; }
.row { display: flex; gap: 8px; align-items: flex-start; }
.row.me { flex-direction: row-reverse; }
.av { width: 38px; height: 38px; border-radius: 50%; flex: none; display: flex;
  align-items: center; justify-content: center; color: #fff; font-size: 15px;
  font-weight: 600; overflow: hidden; background: #fff; }
.av img { width: 100%; height: 100%; object-fit: cover; }
.col { display: flex; flex-direction: column; gap: 4px; max-width: 78%; }
.me .col { align-items: flex-end; }
.name { font-size: 12px; color: #8a8f99; display: flex; gap: 4px; align-items: center; }
.tag { font-size: 10px; padding: 0 4px; border-radius: 3px; line-height: 15px; color: #fff; }
.tag.bot { background: #3d8bfd; } .tag.admin { background: #19b35a; } .tag.owner { background: #ff9f1a; }
.bubble { background: #fff; color: #1f2329; padding: 9px 12px; border-radius: 12px;
  font-size: 15px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
.me .bubble { background: #0099ff; color: #fff; }
.bubble a { color: #1677ff; text-decoration: none; }
.me .bubble a { color: #fff; text-decoration: underline; }
.quote { font-size: 13px; line-height: 1.4; padding: 6px 8px; border-radius: 6px;
  margin-bottom: 6px; background: rgba(0,0,0,.06); color: #5c6270; white-space: normal; }
.me .quote { background: rgba(255,255,255,.2); color: #e8f4ff; }
.hl { background: #fff4c2; border-radius: 3px; padding: 0 2px; }
.pic { width: 64px; height: 64px; border-radius: 10px; display: block; margin-bottom: 6px;
  background: #eaf6ff; }
.system { align-self: center; font-size: 12px; color: #8a8f99; background: #e8e9ec;
  padding: 3px 10px; border-radius: 10px; }
"""


def _icon_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(ICON.read_bytes()).decode()


def avatar(who: str) -> str:
    if who == "bot":
        return f'<div class="av"><img src="{_icon_uri()}"></div>'
    color, letter = {"me": ("#ff7a45", "我"), "admin": ("#36b37e", "管"),
                     "user": ("#9254de", "主")}[who]
    return f'<div class="av" style="background:{color}">{letter}</div>'


def msg(who: str, text: str, *, name: str = "", tag: str = "", quote: str = "",
        pic: bool = False, raw: bool = False) -> str:
    me = who == "me"
    tag_html = ""
    if tag:
        cls = {"机器人": "bot", "管理员": "admin", "群主": "owner"}[tag]
        tag_html = f'<span class="tag {cls}">{tag}</span>'
    head = f'<div class="name">{tag_html}{html.escape(name)}</div>' if name else ""
    body = text if raw else html.escape(text)
    if quote:
        body = f'<div class="quote">{html.escape(quote)}</div>{body}'
    if pic:
        body = f'<img class="pic" src="{_icon_uri()}">{body}'
    return (f'<div class="row{" me" if me else ""}">{avatar(who)}'
            f'<div class="col">{head}<div class="bubble">{body}</div></div></div>')


def phone(title: str, items: list[str], sub: str = "") -> str:
    sub_html = f'<span class="sub">{sub}</span>' if sub else ""
    return (f'<div class="phone"><div class="bar"><span class="back">‹</span>'
            f'{html.escape(title)}{sub_html}</div><div class="chat">{"".join(items)}</div></div>')


def info_private() -> str:
    lines = ["【QQBot-OneBot 信息】", "AppID: 102000001", "BotQQ: 3889000001",
             f'用户ID: <span class="hl">{USER_ID}</span>', "时间: 2026-09-29 12:00:00",
             "尚未设置超级用户: 把上面的用户ID填进 superusers"]
    return "\n".join(lines)


def info_group() -> str:
    lines = ["【QQBot-OneBot 信息】", "AppID: 102000001", "BotQQ: 3889000001",
             "群名: 摸鱼小队", "群人数: 42", "群简介: (未设置)", f"用户ID: {USER_ID}",
             "角色: 管理员", "主动消息: 已开通", "消息接收: 全部消息", "Bot角色: 普通成员",
             "白名单: 未启用", f'启用码: <span class="hl">{html.escape(CODE)}</span>',
             "时间: 2026-09-29 12:05:00"]
    return "\n".join(lines)


SCENES = {
    "demo_setup": phone(BOT, [
        '<div class="time">12:00</div>',
        msg("me", "获取信息"),
        msg("bot", info_private(), raw=True),
    ]),
    "demo_create": phone(BOT, [
        '<div class="time">12:01</div>',
        msg("me", "创建bot"),
        msg("bot", '请 bot 的创建者点击链接绑定自己的 bot：\n<a>请点击 qq</a>', raw=True),
        '<div class="system">对方用手机 QQ 打开链接并授权</div>',
        msg("bot", "已接入: 阿明的机器人  QQ 3889000002\nAppID: 102000002\n号主？", pic=True),
        msg("me", "10001", quote="小Q: 已接入: 阿明的机器人 … 号主？"),
        msg("bot", "✅ 完成"),
    ]),
    "demo_info": phone("摸鱼小队", [
        '<div class="time">12:05</div>',
        msg("admin", f"@{BOT} 获取信息", name="阿明", tag="管理员"),
        msg("bot", info_group(), name=BOT, tag="机器人", raw=True),
    ], sub="(42)"),
    "demo_enable": phone(BOT, [
        '<div class="time">12:06</div>',
        msg("me", f"启用 {CODE}"),
        msg("bot", f"已启用: bot {BOT_ID} × 群 {GROUP_ID}"),
    ]),
}


def render() -> None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome is None:
        raise SystemExit("需要 google-chrome 或 chromium")
    from PIL import Image  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in SCENES.items():
            page = Path(tmp) / f"{name}.html"
            page.write_text(f"<!doctype html><meta charset=utf-8><style>{CSS}</style>"
                            f'<body style="padding:0">{body}</body>', encoding="utf-8")
            shot = Path(tmp) / f"{name}.png"
            subprocess.run(
                [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                 "--force-device-scale-factor=2", "--window-size=392,1600",
                 "--default-background-color=00000000", f"--screenshot={shot}",
                 page.as_uri()], check=True, capture_output=True, timeout=60)
            image = Image.open(shot).convert("RGBA")
            bbox = image.getchannel("A").getbbox()        # 裁透明留白
            image.crop(bbox).save(OUT / f"{name}.png", optimize=True)
            print("wrote", OUT / f"{name}.png")


if __name__ == "__main__":
    render()
