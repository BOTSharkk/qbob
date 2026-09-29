"""启用/禁用: 跨 bot 给群加白/移出白名单, 仅本 bot su.

参数与被引用消息合并提取(参数优先), 粘贴或引用「获取信息」输出均可。
"""

from __future__ import annotations

import logging
import re

from .base import CommandContext, command

logger = logging.getLogger("qqbot.builtin")

ENABLE_CODE_RE = re.compile(r"bot=(\d{15})\D+group=(\d{15})")
BOTID_RE = re.compile(r"BotID[:：]\s*(\d{15})")
GROUPID_RE = re.compile(r"群ID[:：]\s*(\d{15})")


def extract_ids(text: str) -> tuple[int, int] | None:
    match = ENABLE_CODE_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    bot_match, group_match = BOTID_RE.search(text), GROUPID_RE.search(text)
    if bot_match and group_match:
        return int(bot_match.group(1)), int(group_match.group(1))
    return None


@command("启用", "禁用")
async def cmd_toggle(ctx: CommandContext) -> bool:
    if not ctx.is_su:
        # 非 su 当普通消息转发
        return False

    texts = [ctx.arg]
    quoted = await ctx.quoted_text()
    if quoted and quoted != ctx.arg:
        texts.append(quoted)
    ids = extract_ids("\n".join(t for t in texts if t))
    if ids is None:
        await ctx.reply(f"{ctx.command}失败：未识别到 BotID/群ID，"
                        "请粘贴或引用「获取信息」的完整输出")
        return True
    bot_virtual, group_virtual = ids

    idmap = ctx.bot.idmap
    bot_entry = idmap.lookup_virtual(bot_virtual)
    group_entry = idmap.lookup_virtual(group_virtual)
    if bot_entry is None or bot_entry.kind != "bot":
        await ctx.reply(f"{ctx.command}失败：BotID {bot_virtual} 未注册")
        return True
    if (group_entry is None or group_entry.kind != "group"
            or group_entry.bot_appid != bot_entry.bot_appid):
        await ctx.reply(f"{ctx.command}失败：群ID {group_virtual} 与目标 bot 不匹配")
        return True

    # 目标 bot 是「总是启用」(黑名单)时, 启用/禁用改的是黑名单 —— 改白名单在那种模式下没有效果
    target = bot_entry.bot_appid
    access, openid = ctx.bot.access, group_entry.openid
    note = dict(virtual_id=group_virtual, note=f"启用指令 via bot {ctx.bot.appid}",
                added_by=str(ctx.peer_openid))
    done_text = f"已{ctx.command}: bot {bot_virtual} × 群 {group_virtual}"
    if await _group_list_mode(ctx, target) == "black":
        if ctx.command == "启用":
            changed = await access.remove(target, "group", "black", openid)
            text = done_text if changed else f"该群在 bot {bot_virtual} 下本来就是启用的"
        else:
            changed = await access.add(target, "group", "black", openid, **note)
            text = done_text if changed else f"该群已在 bot {bot_virtual} 下被禁用"
    elif ctx.command == "启用":
        changed = await access.add(target, "group", "white", openid, **note)
        text = done_text if changed else f"该群已在 bot {bot_virtual} 的白名单中"
    else:
        changed = await access.remove(target, "group", "white", openid)
        text = done_text if changed else f"该群不在 bot {bot_virtual} 的白名单中"
    await ctx.reply(text)


async def _group_list_mode(ctx: CommandContext, appid: str) -> str:
    """目标 bot 的群启用方式; 它可能没在跑, 所以查库."""
    manager = getattr(ctx.bot, "manager", None)
    live = manager.get_bot(appid) if manager is not None else None
    if live is None and appid == ctx.bot.appid:
        live = ctx.bot
    if live is not None:
        return live.cfg.get("group_list_mode", "white")
    row = await ctx.bot.db.fetchone("SELECT group_list_mode FROM bots WHERE appid=?", (appid,))
    return (row["group_list_mode"] if row else "") or "white"
    return True
