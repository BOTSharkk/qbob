"""适配器内置指令(不经 OneBot 转发, 不受黑白名单限制).

加新指令: 新建模块用 `@command("触发词")` 装饰 `async def handler(ctx) -> bool`,
再在下面 import; 返回 True 表示消费(不转发). 引用型交互用 `@reply_hook`。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import (  # noqa: F401
    CommandContext, command, get_handler, match_builtin, reply_hook, reply_hooks,
)

# 注册各指令(import 即注册, 顺序无关)
from . import create_bot, info, recall, toggle  # noqa: F401,E402

if TYPE_CHECKING:
    from ..bot import BotInstance

logger = logging.getLogger("qqbot.builtin")


class BuiltinCommands:
    """每个 bot 一份的内置指令调度器."""

    def __init__(self, bot: "BotInstance"):
        self.bot = bot
        self.state: dict = {}

    async def handle(
        self,
        command: str,
        arg: str,
        chat_type: str,
        peer_openid: str,
        user_openid: str,
        user_virtual: int,
        member_role: str,
        reply_mid: int,
        quoted_mid: int = 0,
    ) -> bool:
        """返回 True 表示已消费(不再转发给后端). quoted_mid: 被引用消息的 mid."""
        handler = get_handler(command)
        if handler is None:
            return False
        ctx = CommandContext(
            bot=self.bot, command=command, arg=arg, chat_type=chat_type,
            peer_openid=peer_openid, user_openid=user_openid,
            user_virtual=user_virtual, member_role=member_role,
            reply_mid=reply_mid, quoted_mid=quoted_mid, state=self.state,
        )
        try:
            return await handler(ctx)
        except Exception:
            logger.exception("内置指令 %s 执行失败", command)
            return True     # 出错也不再转发

    async def handle_reply(
        self, text: str, chat_type: str, peer_openid: str, user_openid: str,
        user_virtual: int, member_role: str, reply_mid: int, quoted_mid: int,
    ) -> bool:
        """无触发词但引用了消息时, 交给 reply_hook 处理."""
        if not quoted_mid:
            return False
        ctx = CommandContext(
            bot=self.bot, command=text, arg=text, chat_type=chat_type,
            peer_openid=peer_openid, user_openid=user_openid,
            user_virtual=user_virtual, member_role=member_role,
            reply_mid=reply_mid, quoted_mid=quoted_mid, state=self.state,
        )
        for hook in reply_hooks():
            try:
                if await hook(ctx):
                    return True
            except Exception:
                logger.exception("内置指令 reply hook 执行失败")
        return False
