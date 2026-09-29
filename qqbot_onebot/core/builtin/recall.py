"""一次性撤回: 引用转发拆分时补发的 RECALL_HINT 提示, 整批撤回.

只认提示消息本身, 引用转发正文不触发; 命中即消费, 任何人可触发(只撤 bot 自己的)。
"""

from __future__ import annotations

import logging
import time

from ...qq.api import QQApiError
from ..sender import RECALL_HINT_MARK
from .base import CommandContext, reply_hook

logger = logging.getLogger("qqbot.builtin")

# 平台撤回接口错误码
CODE_NO_PERMISSION = 40062003     # 无操作权限
CODE_TOO_OLD = 40064004           # 超出 2 分钟撤回时限
# 非群管 bot 只能撤 2 分钟内的消息(群管不受限, 实测); 超窗直接拦, 不逐条试
RECALL_WINDOW = 120
RECALL_GRACE = 10                 # 时钟与网络余量


@reply_hook
async def on_recall_request(ctx: CommandContext) -> bool:
    record = await ctx.bot.store.get_by_mid(ctx.quoted_mid)
    if record is None:
        return False
    text = "".join(
        seg["data"].get("text", "")
        for seg in record.get("content", [])
        if isinstance(seg, dict) and seg.get("type") == "text"
    )
    if RECALL_HINT_MARK not in text:
        return False
    if record.get("direction") != "out":
        # 平台换 idx 时引用会落到 direction=in 的影子记录, 按内容找回真实那批
        real = await ctx.bot.db.fetchone(
            "SELECT mid, batch_mid FROM messages WHERE bot_appid=?"
            " AND direction='out' AND chat_type=? AND peer_openid=?"
            " AND content LIKE ? ORDER BY mid DESC LIMIT 1",
            (ctx.bot.appid, ctx.chat_type, ctx.peer_openid,
             f"%{RECALL_HINT_MARK}%"))
        if real is None:
            return False
        record = {**record, "mid": real["mid"], "batch_mid": real["batch_mid"],
                  "ts": record.get("ts", 0)}

    # 先查 bot 是否群管: 群管不受 2 分钟限制
    role = (await ctx.bot.group_role(ctx.peer_openid)
            if ctx.chat_type == "group" else "")
    is_admin = role in ("admin", "owner")
    tail = "" if is_admin else "，请设置Bot为管理员后重试"

    age = int(time.time()) - int(record.get("ts") or 0)
    if not is_admin and age > RECALL_WINDOW + RECALL_GRACE:
        await ctx.reply(f"已超过 2 分钟{tail}")
        return True

    batch = record.get("batch_mid") or ctx.quoted_mid
    rows = await ctx.bot.db.fetchall(
        "SELECT mid, chat_type, peer_openid, qq_msg_id FROM messages"
        " WHERE bot_appid=? AND batch_mid=? AND qq_msg_id!=''"
        " ORDER BY mid DESC",
        (ctx.bot.appid, batch),
    )
    targets = [r for r in rows if not r["qq_msg_id"].startswith("unknown-")]
    if not targets:
        await ctx.reply("没找到可撤回的消息")
        return True

    done, too_old, denied, other = 0, 0, 0, 0
    for row in targets:
        try:
            if row["chat_type"] == "group":
                await ctx.bot.api.recall_group_message(
                    row["peer_openid"], row["qq_msg_id"])
            else:
                await ctx.bot.api.recall_c2c_message(
                    row["peer_openid"], row["qq_msg_id"])
            done += 1
        except QQApiError as exc:
            if exc.code == CODE_TOO_OLD:
                too_old += 1
            elif exc.code == CODE_NO_PERMISSION:
                denied += 1
            else:
                other += 1
                logger.warning("[%s] 撤回失败 %s: %s", ctx.bot.appid,
                               row["mid"], exc)

    if done:
        await ctx.bot.db.execute(
            "UPDATE messages SET recalled_at=? WHERE bot_appid=? AND batch_mid=?",
            (int(time.time()), ctx.bot.appid, batch))

    failed = too_old + denied + other
    if done and failed:
        await ctx.reply(f"已撤回 {done} 条，{failed} 条失败{tail}")
    elif denied:
        await ctx.reply(f"撤回失败：没有操作权限{tail}")
    elif too_old:
        await ctx.reply(f"已超过 2 分钟{tail}")
    elif other:
        await ctx.reply("撤回失败，请稍后重试")
    else:
        logger.info("[%s] 一次性撤回 %d 条", ctx.bot.appid, done)
    return True
