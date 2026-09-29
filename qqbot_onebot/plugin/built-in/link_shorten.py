"""长链接收短: 改成可点的 [请点击 域名](url).

只改 markdown 版本, 降级为纯文本时仍是原链接; 按最长链接判断, 整条全收或全不收。
"""

from qqbot_onebot.core.sender import SHORTEN_URL_MIN, shorten_urls_md
from qqbot_onebot.plugin import ConfigField, Plugin

plugin = Plugin(
    name="link_shorten",
    title="长链接收短",
    description="长链接显示成「请点击 域名」的可点链接(需要 markdown)",
    config=[
        ConfigField("min_length", "长度阈值", type="int", default=SHORTEN_URL_MIN,
                    help="消息里最长的链接达到这么多字符才收短"),
    ],
)


@plugin.hook("text_out")
async def shorten(ctx, text):
    return shorten_urls_md(text, int(plugin.config.get("min_length") or SHORTEN_URL_MIN))
