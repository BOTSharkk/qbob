"""长文本 -> 图片(markdown 渲染).

超过 TEXT_IMAGE_MIN_CHARS 的文本由 sender 渲染成图(样式对齐 htmlrender 的 md_to_pic,
宽 500 起、2x, 字多时加宽到 800 内); 链接点不了, 由 sender 摘前几个放进 content.

- markdown -> HTML: web/static/chat.js renderMarkdown 的移植, 行内元素先占位, 骨架转义后再做强调.
- HTML -> PNG: 无头 chrome 单遍大窗截图, Pillow 裁空白. 不做先量高再截: 矮窗口会触发
  font boosting, 两遍布局对不上.

任何失败返回 None, 调用方发原文.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger("qqbot.textimg")

# 原文超过此字符数转图片
TEXT_IMAGE_MIN_CHARS = 300
# 摘进 content 的链接数上限
TEXT_IMAGE_MAX_LINKS = 5

_CSS_PATH = Path(__file__).parent / "assets" / "github-markdown-light.css"
_CHROME = shutil.which("google-chrome") or shutil.which("chromium")
# 同 md_to_pic: 宽 500(命中 <767px 媒体查询, 内边距 15px), 2x.
# 字多时宽度在 [_WIDTH, _MAX_WIDTH] 内自适应, 使高宽比不超过 _TARGET_RATIO.
_WIDTH = 500
_MAX_WIDTH = 800
_TARGET_RATIO = 1.5
_SCALE = 2
_MAX_HEIGHT = 8000
_TIMEOUT = 15

# 并发 chrome 上限
_sem = asyncio.Semaphore(2)
# 同文本不重复渲染
_cache: OrderedDict[str, bytes] = OrderedDict()
_CACHE_MAX = 16

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\((https?://[^\s)]+)\)")
# URL 末尾粘着的句末标点, 不算进链接
_URL_TAIL_PUNCT = "。，、；：！？…）】》」』.,;:!?"


def extract_links(text: str, limit: int = TEXT_IMAGE_MAX_LINKS) -> list[str]:
    """按出现顺序取前 limit 个链接(markdown 链接取目标), 去重."""
    seen: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_URL_TAIL_PUNCT)
        # 裸 URL 正则同样能命中 markdown 链接目标
        if url and url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen


# markdown 语法探测: 出站是否走 msg_type=2 与转发网页是否按 markdown 渲染共用, 须保持一致
_MD_PATTERNS = [
    r"^#{1,6}\s+\S",              # 标题
    r"\*\*[^\s*][^*]*\*\*",       # 粗体
    r"~~[^\s~][^~]*~~",           # 删除线
    r"!\[[^\]]*\]\([^)]+\)",      # 图片
    r"(?<!!)\[[^\]]+\]\([^)]+\)",  # 链接
    r"^\s*[-*+]\s+\S",            # 无序列表
    r"^\s*\d+\.\s+\S",            # 有序列表
    r"^\s*>\s+\S",                # 引用
    r"^\s*(?:\*\s*){3,}$",        # 分割线
    r"`[^`\n]+`",                 # 行内代码
    r"^```",                      # 代码块
    r"^\s*\|.+\|\s*$",            # 表格
]
_MD_RE = re.compile("|".join(_MD_PATTERNS), re.MULTILINE)


def looks_like_markdown(text: str) -> bool:
    return bool(_MD_RE.search(text))


# ---------------- markdown -> HTML(chat.js renderMarkdown 的移植) ----------------

_ESC_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}


def _esc(text: str) -> str:
    return "".join(_ESC_MAP.get(ch, ch) for ch in text)


_BLOCK_START = re.compile(r"^\s{0,3}(?:```|#{1,6}\s|[-*+]\s|\d+\.\s|>|\|)")
_HR = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$")
_TABLE_SPLIT = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_OL_ITEM = re.compile(r"^\s*\d+\.\s+(.*)$")
_UL_ITEM = re.compile(r"^\s*[-*+]\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_QUOTE = re.compile(r"^\s{0,3}>")


def _emphasis(escaped: str) -> str:
    """强调替换, 只作用于已转义的串."""
    out = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", escaped)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(^|[^*\w])\*([^*\n]+)\*(?!\*)", r"\1<em>\2</em>", out)
    return out


def _inline(raw: str) -> str:
    """行内: 代码/图片/链接先占位, 其余转义+强调."""
    slots: list[str] = []

    def hold(rendered: str) -> str:
        slots.append(rendered)
        return f"\x00{len(slots) - 1}\x00"

    s = raw.replace("\x00", "")
    s = re.sub(r"`([^`\n]+)`",
               lambda m: hold(f"<code>{_esc(m.group(1))}</code>"), s)
    # 图片已被 sender 抽走, 兜底显示说明文字
    s = re.sub(r"!\[([^\]]*)\]\(\s*[^)]*\)", lambda m: hold(_esc(m.group(1))), s)
    s = re.sub(r"\[([^\]]+)\]\(\s*(https?://[^\s)]+)[^)]*\)",
               lambda m: hold(f'<a href="{_esc(m.group(2))}">'
                              f"{_emphasis(_esc(m.group(1)))}</a>"), s)
    # 句末标点放在链接外, 与 extract_links 一致
    def bare(match: re.Match) -> str:
        url = match.group(0)
        tail = ""
        while url and url[-1] in _URL_TAIL_PUNCT:
            url, tail = url[:-1], url[-1] + tail
        if not url:
            return match.group(0)
        return hold(f'<a href="{_esc(url)}">{_esc(url)}</a>') + tail
    s = re.sub(r"https?://[^\s<>\"'）】]+", bare, s)
    s = _emphasis(_esc(s))
    return re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m.group(1))], s)


def md_to_html(source: str) -> str:
    """极简 markdown -> HTML: 标题/列表/引用/代码块/表格/分割线/段落."""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{_esc(chr(10).join(code))}</code></pre>")
            continue
        if not line.strip():
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue
        if _QUOTE.match(line):
            quoted: list[str] = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                quoted.append(re.sub(r"^\s{0,3}>\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{md_to_html(chr(10).join(quoted))}</blockquote>")
            continue
        if _TABLE_ROW.match(line) and i + 1 < len(lines) \
                and _TABLE_SPLIT.match(lines[i + 1] or ""):
            def cells(row: str) -> list[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]
            head = cells(line)
            i += 2
            body: list[list[str]] = []
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                body.append(cells(lines[i]))
                i += 1
            head_html = "".join(f"<th>{_inline(c)}</th>" for c in head)
            body_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                for row in body)
            out.append(f"<table><thead><tr>{head_html}</tr></thead>"
                       f"<tbody>{body_html}</tbody></table>")
            continue
        ordered = bool(_OL_ITEM.match(line))
        item_re = _OL_ITEM if ordered else _UL_ITEM
        if item_re.match(line):
            items: list[str] = []
            while i < len(lines):
                m = item_re.match(lines[i])
                if not m:
                    break
                items.append(f"<li>{_inline(m.group(1))}</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        # 段落: 单换行也断行(插件按纯文本习惯)
        para: list[str] = []
        while i < len(lines) and lines[i].strip() and not _BLOCK_START.match(lines[i]):
            para.append(lines[i])
            i += 1
        if not para:
            para.append(lines[i])
            i += 1
        out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
    return "".join(out)


# sender 在链接标签后塞的 ⟦n⟧ 渲染成 <sup>[n]</sup>(unicode 上标在 CJK 字体里不齐)
_SUP_TOKEN_RE = re.compile(r"⟦(\d+)⟧")


def build_page(source: str) -> str:
    css = _CSS_PATH.read_text(encoding="utf-8")
    body = _SUP_TOKEN_RE.sub(r"<sup>[\1]</sup>", md_to_html(source))
    # 页内脚本二分出自适应宽度
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style>
<style>
  html, body {{ margin: 0; padding: 0; background: #ffffff; }}
  /* 宽度与内边距钉死, 不跟视口; 15px 即 md_to_pic 在 500 宽下命中的移动端内边距 */
  .markdown-body {{ box-sizing: border-box; width: {_WIDTH}px;
                    margin: 0; padding: 15px; }}
</style></head>
<body><article class="markdown-body">{body}</article>
<script>
/* 自适应宽度: 找最小的宽 W, 使 高/W <= 目标比, 或已与最大宽时一样矮(再加宽
   也不变矮, 如短行列表). 条件对 W 单调, 二分即可; 截图后右侧空白由 Pillow 裁 */
(function () {{
  var el = document.querySelector(".markdown-body");
  function h(w) {{ el.style.width = w + "px"; return el.offsetHeight; }}
  var lo = {_WIDTH}, hi = {_MAX_WIDTH}, floor = h(hi) * 1.02;
  function ok(w) {{ var x = h(w); return x <= w * {_TARGET_RATIO} || x <= floor; }}
  if (ok(lo)) {{ h(lo); return; }}
  while (hi - lo > 10) {{ var mid = (lo + hi) >> 1; if (ok(mid)) hi = mid; else lo = mid; }}
  h(hi);
}})();
</script></body></html>"""


def _crop_bottom(png: bytes) -> bytes | None:
    """裁掉底部与右侧空白, 保留 15px(x2) 内边距; 宽度至少 _WIDTH."""
    from PIL import Image, ImageChops  # noqa: PLC0415 冷路径才 import
    import io  # noqa: PLC0415

    with Image.open(io.BytesIO(png)) as im:
        rgb = im.convert("RGB")
        # 非白内容的包围盒
        bg = Image.new("RGB", rgb.size, (255, 255, 255))
        bbox = ImageChops.difference(rgb, bg).getbbox()
        if bbox is None:
            return None                     # 全白: 渲染不可信
        pad = 15 * _SCALE
        bottom = min(rgb.height, bbox[3] + pad)
        right = min(rgb.width, max(_WIDTH * _SCALE, bbox[2] + pad))
        if bottom >= rgb.height and right >= rgb.width:
            return png
        out = io.BytesIO()
        rgb.crop((0, 0, right, bottom)).save(out, "PNG")
        return out.getvalue()


def _render_sync(page: str) -> bytes | None:
    if _CHROME is None:
        logger.warning("textimg: 找不到 chrome, 长文本保持原样发送")
        return None
    with tempfile.TemporaryDirectory(prefix="qqob-ti-") as tmp:
        html_path = Path(tmp) / "page.html"
        png_path = Path(tmp) / "out.png"
        html_path.write_text(page, encoding="utf-8")
        try:
            shot = subprocess.run(
                [_CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--hide-scrollbars", "--virtual-time-budget=4000",
                 "--run-all-compositor-stages-before-draw",
                 f"--user-data-dir={tmp}/profile",
                 f"--window-size={_MAX_WIDTH},{_MAX_HEIGHT}",
                 f"--force-device-scale-factor={_SCALE}",
                 f"--screenshot={png_path}", html_path.as_uri()],
                capture_output=True, text=True, timeout=_TIMEOUT)
            if shot.returncode != 0 or not png_path.exists():
                logger.warning("textimg: 截图失败 rc=%s %s", shot.returncode,
                               (shot.stderr or "")[-200:])
                return None
            data = png_path.read_bytes()
            if not data.startswith(b"\x89PNG"):
                return None
            return _crop_bottom(data)
        except subprocess.TimeoutExpired:
            logger.warning("textimg: chrome 渲染超时(%ss)", _TIMEOUT)
            return None
        except Exception as exc:            # noqa: BLE001 Pillow 解码等
            logger.warning("textimg: 处理失败 %s", exc)
            return None


async def render_text_image(source: str) -> bytes | None:
    """文本(可含 markdown) -> PNG; 任何失败返回 None, 调用方发原文."""
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if digest in _cache:
        _cache.move_to_end(digest)
        return _cache[digest]
    async with _sem:
        if digest in _cache:            # 排队期间已被渲染
            _cache.move_to_end(digest)
            return _cache[digest]
        data = await asyncio.to_thread(_render_sync, build_page(source))
    if data:
        _cache[digest] = data
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return data
