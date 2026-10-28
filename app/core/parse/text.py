"""纯文本与标记类：txt / md / csv / tsv / html → 块。

## 在流水线里的位置

- **上游**：:func:`app.core.parse.iter_units` 按后缀把这一族格式都派发到这里
  （没有后缀的文件也当纯文本）；
- **本模块**：只负责"字节 → 块 + 定位"，产出 :class:`Unit`。**不做启发式判标题**（md 的
  ``#``、html 的 ``h1``–``h6`` 是文档自己带的原生层级，照实还原）、**不切分**；
- **下游**：统一清洗（:mod:`app.core.parse.clean`）→ 结构识别 → 切分 → 入库。

## 输入输出

- 输入：磁盘路径 + 可选展示名（csv 的 ``table_ref`` 用它）。
- 输出：``Iterator[Unit]``；**能对上源码的块给文件行号**（txt/log/md 的
  ``line_from``/``line_to``，1 基）；html 的行号只是清洗后文本内的行序。
  表格块带 ``table_ref``（csv 是 ``csv:文件名#1``，md 是 ``md:#1``）与表内行区间。

## 关键约定

- **编码先嗅探再解码**：国内机器"另存为 ANSI"（GBK）太常见，写死 utf-8 会**不报错但全乱码**；
- **行号能对上的才给**：md 用 ``token.map``，txt/log 用真实文件行号；html 抽出的文本与源码行对不上，
  给不了"源码行号"（html 的 ``line_from`` 只是清洗后文本内的行序，不是源码行）；
- **块不会在句中截断**：txt 靠增量解码 + 按行累积（见下），csv/html/md 整份读。

**流式口径（用户要求）**：

- **txt / log**：``iter_units(path)`` 按 1 MB 字节块读，**增量解码**（`codecs.IncrementalDecoder`，
  正确处理跨块的多字节中文字符——这就是"字节块不会切断句子"的落地方式），按行累积成段，
  **一次只有一个段落块在内存**；行号 = 文件行号；
- **md**：markdown-it-py 需要整棵 token 树（标题层级是文档级结构），所以整份读；
  块带文件行号（``token.map``）；
- **csv/tsv/html**：整份读（都是小格式），表格整块保留。

**行号是这类格式的定位锚点**：txt/md 的块记录文件行号，引用能说"第 12–20 行"。
"""

from __future__ import annotations

import codecs
import contextlib
import csv
import io
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

from app.core.parse.meta import looks_like_article_meta
from app.core.parse.model import (
    TYPE_CODE,
    TYPE_HEADING,
    TYPE_IMAGE,
    TYPE_TABLE,
    Block,
    ParseError,
    Unit,
)

#: 纯文本类后缀（不需要第三方库）
TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".html", ".htm"})

#: 流式读取的字节块大小（**只用于字节搬运 + 增量解码**，句子完整性由增量解码器与"按行"
#: 两个机制保证，与切分器无关）。
STREAM_CHUNK = 1 << 20

#: 单个块的字符上限。没有空行的超大文本（日志/转写稿）不能攒成一个块，
#: 否则解析层会把整份文件读进内存；到上限就在行边界先产出一个块。
MAX_BLOCK_CHARS = 20_000

#: 日志行的行首时间戳：``[2026-06-12 08:00:00]`` / ``2026-06-12 08:00:00`` / ``2026/06/12 08:00``
_LOG_TS = re.compile(r"^\s*\[?\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}")
#: 判定"这份 txt 其实是日志"时最多看多少行
_LOG_SAMPLE_LINES = 200
#: 命中比例下限：多数行都是时间戳开头才算日志（避免把含日期的正文误判）
_LOG_RATIO = 0.6
#: 少于这么多行就不按日志处理（几行文本不值得逐行成块）
_LOG_MIN_LINES = 5


#: 页面噪声关键词：class/id/role 里出现这些词，整块跳过（导航/页脚/侧栏/面包屑）
_NOISE_TOKENS = frozenset(
    {
        "nav",
        "navbar",
        "navigation",
        "menu",
        "footer",
        "breadcrumb",
        "sidebar",
        "topbar",
        "copyright",
        # 站点自身地址（TRS 等 CMS 常见的页头/页脚占位，如 class="siteurl hide"）
        "siteurl",
        # 角落噪声：打印/语言切换/翻译/分享/字号调整
        "print",
        "translate",
        "search",
        "login",
    }
)
#: "header" 单独判：站点头部（header/header_toolbar/header_search）要跳，
#: 但文章头部（article-header/entry-header）可能含正文标题，不能跳。
_HEADER_HINTS = frozenset({"toolbar", "logo", "search", "menu", "nav"})


class _TagStripper(HTMLParser):
    """把 HTML 变成**有序片段**（丢掉 script/style/导航，块级标签换成换行）。

    与"纯文本"版本的关键区别（这是标题能正确入库的前提）：

    - ``h1``–``h6`` **不再当普通块级标签**：收成 ``(层级, 文本)`` 片段，下游据此造
      ``content_type=heading`` 的块；否则标题只是一行普通文字，结构识别认不出；
    - ``<title>``（在 ``<head>`` 里）**单独收**：``<head>`` 整体会被跳过，但标题元数据是
      文档最重要的身份信息，不能跟着一起丢；它是"正文没有 ``<h1>``"时的兜底标题。

    ``_skip_stack`` 记录**跳过区里所有还开着的标签**（含跳过起点），结束标签按"最近的
    同名标签"匹配：``<div class="header">`` 里套 ``<div>`` 时，内层 ``</div>`` 只关内层，
    不会把整个头部提前放出来。老实现按"栈顶标签名相等"出栈，内层一关就漏出 ``EN``、
    站点 URL 这类页头噪声（实测）。
    """

    _BLOCK: ClassVar[set[str]] = {"p", "div", "br", "li", "tr", "section", "article"}
    #: ``h1``–``h6`` → 层级（标题不是普通块，见 :meth:`handle_starttag`）
    _HEADINGS: ClassVar[dict[str, int]] = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    #: 空元素（没有结束标签）：跳过区里不计入嵌套深度，否则会一直"少关一个"
    _VOID: ClassVar[set[str]] = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    _SKIP: ClassVar[set[str]] = {
        "script",
        "style",
        "noscript",
        "head",
        # **页面导航/页脚/侧栏是噪声**：会被当成正文入库，且常是每页重复的一整块。
        # header 不能跳（可能是正文标题），只跳语义明确的 nav/footer/aside。
        "nav",
        "footer",
        "aside",
    }

    def __init__(self) -> None:
        """初始化收集器：``items`` 是片段（0=正文，1–6=标题层级），``title_parts`` 收 ``<title>``。"""
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[int, str]] = []
        self.title_parts: list[str] = []
        self._buffer: list[str] = []
        self._skip_stack: list[str] = []
        self._heading_level: int | None = None
        self._heading_parts: list[str] = []
        self._in_title = False

    def _is_noise(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        """这个标签是不是**页面噪声**（导航/页脚/侧栏/面包屑）。

        除了标签名（nav/footer/aside），很多站点用 ``<div class="navbar">``：
        按 class/id/role 里的词再判一次，避免整条导航被当正文入库。
        """
        if tag in self._SKIP:
            return True
        raw = " ".join(
            str(value or "") for key, value in attrs if key in ("class", "id", "role")
        ).lower()
        tokens = {token for token in re.split(r"[\s_\-]+", raw) if token}
        if tokens & _NOISE_TOKENS:
            return True
        # 站点头部：class="header" / header_toolbar / header_search / header_logo
        return "header" in tokens and (len(tokens) == 1 or bool(tokens & _HEADER_HINTS))

    def _flush_text(self) -> None:
        """把累积的正文片段收进 ``items``（空片段丢弃）。"""
        text = "".join(self._buffer)
        if text:
            self.items.append((0, text))
        self._buffer = []

    def finish(self) -> list[tuple[int, str]]:
        """收尾：把**最后一段正文**也收进 ``items``，返回全部片段。

        为什么必须有这一步：正文先攒在 ``_buffer`` 里，只在遇到标题/收尾时才落进
        ``items``；不显式收尾的话，最后一个标题之后的正文会整段丢掉（实测）。
        """
        self._flush_text()
        return self.items

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """开始标签：先收 ``<title>``，再进跳过区 / 开标题片段 / 给块级标签补换行。"""
        # ``<title>`` 优先于跳过区判断：它通常在 ``<head>``（跳过区）里，
        # 先判噪声会连整个文档标题一起丢掉（就是"标题存成文件名"的根因）。
        if tag == "title":
            self._in_title = True
            return
        if self._skip_stack:
            # 跳过区里的标签也要记账（嵌套深度），否则同名的内层结束标签会提前关掉跳过区
            if tag not in self._VOID:
                self._skip_stack.append(tag)
            return
        if self._is_noise(tag, attrs):
            if tag not in self._VOID:
                self._skip_stack.append(tag)
            return
        if tag in self._HEADINGS:
            self._flush_text()
            self._heading_level = self._HEADINGS[tag]
            self._heading_parts = []
        elif tag in self._BLOCK:
            self._buffer.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """结束标签：收标题片段 / 退出跳过区 / 块级标签补换行。"""
        if tag == "title":
            self._in_title = False
            return
        if self._skip_stack:
            if tag in self._VOID:
                return
            # 关"最近的同名标签"：`<div class=header><div></div></div>` 里第一个 `</div>`
            # 关的是内层，跳过区必须继续，直到 header 自己的 `</div>`。
            for index in range(len(self._skip_stack) - 1, -1, -1):
                if self._skip_stack[index] == tag:
                    del self._skip_stack[index:]
                    break
            return
        if self._heading_level is not None and self._HEADINGS.get(tag) == self._heading_level:
            text = "".join(self._heading_parts)
            level = self._heading_level
            self._heading_level, self._heading_parts = None, []
            if text:
                self.items.append((level, text))
            return
        if tag in self._BLOCK:
            self._buffer.append("\n")

    def handle_data(self, data: str) -> None:
        """文本节点：``<title>`` 单独收，跳过区丢弃，标题区进标题，其余进正文。"""
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_stack:
            return
        if self._heading_level is not None:
            self._heading_parts.append(data)
            return
        self._buffer.append(data)


def _sniff_encoding(data: bytes) -> str:
    """从文件头一截字节嗅探编码：UTF-8(含BOM) → GBK → 宽松 UTF-8。

    为什么必须嗅探：GBK 是多字节有状态编码，**增量解码**之前得先知道用哪个解码器，
    否则跨块的汉字会被拆开。嗅探只读前 64 KB。

    顺序有讲究：先 ``utf-8-sig``（能解就说明是 UTF-8，顺便吃掉 BOM），再 ``gbk``
    （GBK 几乎什么字节都能"解"出来，放前面会把 UTF-8 误判成 GBK）。**都存在反例**
    （纯 ASCII 用哪个都对；短 GBK 片段也可能恰好是合法 UTF-8），所以最后兜底返回
    ``utf-8`` 并用 ``errors="replace"``——宁可个别字变替换符，也不整篇乱码。

    参数：
        data: 文件头若干字节（调用方负责截断，别传整份文件）。
    返回：
        编码名：``utf-8-sig`` / ``gbk`` / ``utf-8``。
    """
    head = data[: 64 * 1024]
    for encoding in ("utf-8-sig", "gbk"):
        try:
            head.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _read_text(path: Path) -> str:
    """整份读文本：**先嗅探编码再解码**（坏字节退化为替换符，绝不静默用错编码）。

    为什么必须嗅探：md/csv/html 都是整份读，早期版本写死 ``utf-8-sig``——
    GBK 文件**不报错但全是乱码**（实测复现），直接违反"数据精准是一切的基础"。
    国内机器"另存为 ANSI"太常见，这里与 txt 流式路径用同一套 ``_sniff_encoding``。

    参数：
        path: 文本文件路径。
    返回：
        解码后的全文；坏字节变成 U+FFFD 而不是抛异常（整份读的格式没必要为此中断）。
    异常：
        OSError: 文件读不了（由调用方决定是否包成 ParseError）。
    """
    data = path.read_bytes()
    encoding = _sniff_encoding(data)
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        # 头部能解、全文解不了（坏字节在后面）：退化成替换符，至少不炸、不串编码
        return data.decode(encoding, errors="replace")


def clean(text: str) -> str:
    """压掉多余空白：连续空行变一个，行尾空格去掉。

    这是**格式内部的排版清理**（不是 :mod:`app.core.parse.clean` 的不可见字符清洗）：
    只动空行与行尾空白，不动字与字之间的空格——中文排版里的空格有时是有意义的，
    删了会让"第 3 条"和"第3条"在检索上对不上。

    参数：
        text: 原始文本（CRLF / CR 都行）。
    返回：
        统一成 ``\\n`` 换行、行尾无空格、连续空行压成一个、首尾去空白的文本。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _md_inline(text: str) -> str:
    """去掉 Markdown **行内标记**（标题/表格单元格用）：``**粗体**``、`` `code` ``、链接。

    只在"结构文本"（标题）上做：正文字段要保留原文，不能在这里改写。
    """
    value = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [文字](链接) → 文字
    value = value.replace("**", "").replace("__", "").replace("`", "")
    value = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", value)  # *斜体* → 斜体
    value = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"\1", value)  # _斜体_ → 斜体
    return value.strip()


def _paragraph_blocks(lines: list[str], start_no: int = 1) -> list[Block]:
    """把若干行按空行分组（txt/log 的流式消费方），块带**文件行号**。

    ``start_no`` 是这批第一行的真实文件行号（流式时批次从任意行开始，不能从 1 数）。

    分组规则：**空行是段落边界**（作者写的空行就是语义边界）；非空行累积成一个块，
    ``line_from``/``line_to`` 记首末行。块文本 ``strip()`` 过——行尾空白对检索没意义。

    参数：
        lines: 相邻的若干行（不含换行符），顺序即文件顺序。
        start_no: 第一行的文件行号（1 基）。
    返回：
        按空行切出的块列表；全空行时返回空列表。
    """
    blocks: list[Block] = []
    current: list[str] = []
    start: int | None = None
    for offset, line in enumerate(lines):
        line_no = start_no + offset
        if line.strip():
            if start is None:
                start = line_no
            current.append(line)
        elif current:
            # 空行结束当前段：line_to 取最后一行**非空**行（不是这个空行）
            blocks.append(Block(text="\n".join(current).strip(), line_from=start, line_to=line_no - 1))
            current, start = [], None
    if current:
        # 收尾：这批最后一段没有空行结尾（流式批次 / 文件末尾）
        blocks.append(Block(text="\n".join(current).strip(), line_from=start, line_to=start_no + len(lines) - 1))
    return blocks


def _without_meta(blocks: list[Block]) -> list[Block]:
    """去掉**文章元信息**块（日期/来源/责任编辑）：它们是页面信息，不是正文。

    HTML 在造块时逐段过滤；txt/log 走这里统一过滤，避免"2024-03-15 来源：…"
    成为第一个正文片段的开头。
    """
    return [block for block in blocks if not looks_like_article_meta(block.text)]


def _iter_text_lines(path: Path) -> Iterator[tuple[int, str]]:
    """流式读纯文本的行：``(文件行号, 行文本)``。

    **句子不完整的两层保证**：
    1. 增量解码器（utf-8/gbk 都是多字节编码）保证跨 1 MB 块的多字节字符不被拆开；
    2. 行是自然的句子边界载体，跨块的行尾会被 carry 到下个块——绝不半行。

    实现细节：``pending`` 存"上一块末尾那半行"，与下一块解码结果拼起来再切行；
    只有读到 EOF 才把 ``pending`` 当最后一行产出（否则会把一行拆成两块）。

    参数：
        path: 纯文本文件。
    返回：
        ``(行号, 行文本)`` 的流；行号从 1 开始连续，行内不含 ``\\n``。
    异常：
        OSError: 文件读不了。
    """
    with path.open("rb") as handle:
        head = handle.read(64 * 1024)
        encoding = _sniff_encoding(head)
        handle.seek(0)
        decoder = codecs.getincrementaldecoder(encoding)()
        # 宽松 UTF-8 兜底时用 errors="replace"，别的编码严格（sniff 已确认能解）
        if encoding == "utf-8":
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        pending: str = ""
        line_no = 0
        while True:
            chunk = handle.read(STREAM_CHUNK)
            if not chunk:
                break
            text = pending + decoder.decode(chunk)
            # **行分隔符统一**：老 Mac 文本只有 ``\r``，不认它的话整份文件会变成"一行"——
            # 内容是保住了，但引用只能显示"第 1 行"（实测）。CRLF 先在块内合并，
            # 再处理孤立 CR；末尾孤零零的 ``\r`` 可能是被块切开的 CRLF，留到下个块再说。
            text = text.replace("\r\n", "\n")
            carry_cr = ""
            if text.endswith("\r"):
                text, carry_cr = text[:-1], "\r"
            text = text.replace("\r", "\n")
            lines = text.split("\n")
            pending = lines.pop() + carry_cr  # 可能不完整的最后一行 → 等下个块
            for line in lines:
                line_no += 1
                yield line_no, line
        if pending.strip():
            line_no += 1
            yield line_no, pending


def iter_units(path: Path, name: str | None = None) -> Iterator[Unit]:
    """按结构单元产出（txt/log 逐段流式；md/csv/html 整份）。

    ``name``：**展示用文件名**（csv 的 table_ref 会用它）。为什么单独传：
    导入时文件先落在 inbox（``{uuid}_{原名}``），``path.name`` 带着暂存前缀，
    直接用会把 ``csv:ab12cd_清单.csv#1`` 这样的东西带进引用（实测踩过）。

    **后缀按展示名判断**：展示名是调用方（导入/任务层）认为的用户文件名，格式路由听它的。
    暂存前缀不影响后缀，所以两者通常一致；真不一致时，以用户看到的那个为准。

    参数：
        path: 磁盘文件。
        name: 展示用文件名；只影响 ``table_ref`` 里的名字与后缀路由。
    返回：
        单元流；每单元内块有序。
    异常：
        ParseError: 缺 markdown-it-py（见 md 分支）。
    """
    display = name or path.name
    suffix = ("." + display.rsplit(".", 1)[-1].lower()) if "." in display else ""
    if suffix in (".csv", ".tsv"):
        # 整份一张表：表格块必须整块保留（不能按行拆），所以一个单元一个块
        yield _csv_unit(path, display)
        return
    if suffix in (".md", ".markdown"):
        yield _md_unit(path)
        return
    if suffix in (".html", ".htm"):
        yield _html_unit(path)
        return
    if suffix == ".log":
        # 日志是**行式**数据：一行一条独立事件，不能按空行/句子攒成一大段
        yield from _log_units(path)
        return
    # 其余（.txt / 无后缀）：**内容像日志就按行**，否则仍按段落
    if _looks_like_log(path):
        yield from _log_units(path)
        return
    yield from _txt_units(path)


def _looks_like_log(path: Path) -> bool:
    """这份纯文本是不是**日志型**（多数非空行以时间戳开头）。

    为什么要判：``.log`` 已明确按行解析，但很多日志被存成 ``.txt``（或没有后缀）。
    按段落解析时它们没有空行、没有句末标点，会变成一个大块、切分落到半行上。
    判据**保守**，只在前 200 行里"多数行是时间戳开头"时才按日志处理：

    - 至少 ``_LOG_MIN_LINES`` 个非空行（几行文本不值得逐行）；
    - 命中比例 ≥ ``_LOG_RATIO``（含日期的正文不会误判）。

    只读文件头 64 KB，不整份读；读不了就返回 False（退回段落逻辑，不阻断导入）。

    参数：
        path: 纯文本文件。
    返回：
        像日志返回 True。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(64 * 1024)
    except OSError:
        return False
    text = head.decode(_sniff_encoding(head), errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()][:_LOG_SAMPLE_LINES]
    if len(lines) < _LOG_MIN_LINES:
        return False
    matched = sum(1 for line in lines if _LOG_TS.match(line))
    return matched / len(lines) >= _LOG_RATIO


def _log_units(path: Path) -> Iterator[Unit]:
    """log：**一行一个块**（每行是一条独立事件）。

    为什么单独走：日志行几乎都不以句末标点结尾，按"空行分段 + 句子切分"会把十几行
    并成一个大块，切分器只能在逗号处切开——子块边界落在半条日志上，引用读不通。
    一行一块之后，切分器按行边界分组（父块仍会把相邻行聚成证据块）。

    适用：``.log`` 一律走这里；``.txt`` / 无后缀由 :func:`_looks_like_log` 判定后走这里。

    参数：
        path: 纯文本日志文件。
    返回：
        单元流；每个非空行一个块，带**文件行号**（1 基）。
    """
    for line_no, line in _iter_text_lines(path):
        text = line.strip()
        if text:
            yield Unit(blocks=[Block(text=text, line_from=line_no, line_to=line_no)])


def _txt_units(path: Path) -> Iterator[Unit]:
    """txt/log：**流式**，一次一个段落块（行号是真实文件行号）。

    单块有**字符上限**：整份文件没有空行时（日志、转写稿），不能把全文攒成一个块，
    否则解析层就要把整份文件读进内存。到上限就在**行边界**先产出一个块。

    两个触发点：遇到空行（自然段落边界）或累积字符数 ≥ ``MAX_BLOCK_CHARS``。
    后者会**把一段切成多个块**，但切线落在行边界上，所以不会切碎句子。

    参数：
        path: 纯文本文件。
    返回：
        单元流；**每个单元一个块**（一段连续非空行）。空行是单元边界。
    """
    lines: list[str] = []
    first_no: int | None = None
    size = 0

    for line_no, line in _iter_text_lines(path):
        if line.strip():
            if first_no is None:
                first_no = line_no
            lines.append(line)
            size += len(line) + 1  # +1 是换行符：块大小按原文计，别少算
            if size >= MAX_BLOCK_CHARS:
                yield Unit(blocks=_without_meta(_paragraph_blocks(lines, first_no)))
                lines, first_no, size = [], None, 0
        elif lines:
            yield Unit(blocks=_without_meta(_paragraph_blocks(lines, first_no)))
            lines, first_no, size = [], None, 0
    if lines:
        # 文件末尾没有空行：收掉最后一批
        yield Unit(blocks=_without_meta(_paragraph_blocks(lines, first_no)))


def _csv_unit(path: Path, name: str) -> Unit:
    """CSV/TSV：整份就是**一张表**（表格整块保留，行区间进块）。

    ``name`` 是展示用文件名（table_ref 用它，不带暂存前缀）。

    细节：空单元格被丢掉（``if cell and cell.strip()``）、整行空的跳过——渲染成
    ``a | b | c`` 时不会出现 ``a |  | c`` 这种空洞；代价是列对齐信息丢了
    （原表哪一列是空的看不出来），对检索够用、对还原表格不够。

    ``row_from``/``row_to`` 是**表内行号**（1 基，跳过空行后连续），不是文件行号——
    csv 没有可靠的文件行号（引号内可以有换行）。

    参数：
        path: csv/tsv 文件。
        name: 展示用文件名。
    返回：
        单个单元的流（空文件返回空块列表的单元）。
    异常：
        OSError: 文件读不了。
    """
    suffix = name.rsplit(".", 1)[-1].lower()
    delimiter = "\t" if suffix == "tsv" else ","
    rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(_read_text(path)), delimiter=delimiter):
        cells = [cell.strip() for cell in row]
        if any(cells):
            rows.append(cells)
    if not rows:
        # 空表/全是空行：返回空单元而不是报错（空文件是"没内容"，不是"解析失败"）
        return Unit(blocks=[])
    # 空列保留（CSV 允许稀疏行），所有行补齐到最大列数
    max_col = max(len(row) for row in rows)
    padded = [row + [""] * (max_col - len(row)) for row in rows]
    rendered = "\n".join(" | ".join(row) for row in padded)
    return Unit(
        blocks=[
            Block(
                text=rendered,
                content_type=TYPE_TABLE,
                table_ref=f"csv:{name}#1",
                table_title=str(name),
                row_from=1,
                row_to=len(padded),
                col_from=1,
                col_to=max_col,
                header=tuple(padded[0]),
            )
        ]
    )


def _strip_site_suffix(title: str) -> str:
    """去掉 ``<title>`` 里常见的"站点名尾巴"（如 ``…__中国政府网``）。

    很多站点把标题写成"文章标题__站点名"或"文章标题 - 站点名"；直接当文档标题会让
    界面出现一长串站点后缀。只认**双下划线**与"空格-空格"这两种**明确的站点分隔**，
    单下划线/破折号在标题正文里太常见，不碰。

    参数：
        title: ``<title>`` 的原文。
    返回：
        去掉站点尾巴并 ``strip`` 的标题。
    """
    value = re.sub(r"\s*_{2}[^_]{1,30}$", "", title)
    value = re.sub(r"\s+[-–—]\s+[^-–—]{1,30}$", "", value)
    return value.strip()


def _html_unit(path: Path) -> Unit:
    """HTML → 块（去掉标签与 script/style/导航，``h1``–``h6`` 还原成标题块）。

    **文档标题**：优先取正文第一处 ``<h1>``（站点正文标题通常干净），没有就退回
    ``<head><title>``（去掉"__站点名"尾巴）。两者都作为**首块 H1** 产出，并去掉正文里
    同文的重复 ``<h1>``——这样结构识别能把它记成 ``doc_title``，界面/引用才有真标题；
    否则整份文档没有原生 H1，导入层会拿**文件名**兜底（``doc.htm`` → 标题变成 "doc"）。
    首块带 ``doc_title_hint``：``<title>`` 是站点显式给的标题，长度/编号规则不该反过来否掉它。
    **文章元信息**（``2026-10-18 08:40 / 来源：人民日报``）会被识别并跳过：它是页面信息，
    不该做正文片段的开头（见 :func:`app.core.parse.meta.looks_like_article_meta`）。

    **不给"源码行号"**：抽出来的文本顺序与源码行对不上（标签、属性、换行都在变），
    编一个源码行号只会让引用跳错地方；诚实地说"没有源码行定位"比错位好。
    块上的 ``line_from`` 是**清洗后文本内的行序**（能用于块内定位，跳回源码则不准）。

    参数：
        path: html 文件。
    返回：
        单个单元（块按块级标签/空行分段；``h1``–``h6`` 是 heading 块）。
    异常：
        OSError: 文件读不了。
    """
    stripper = _TagStripper()
    stripper.feed(_read_text(path))
    items = stripper.finish()

    # 标题优先正文第一个 <h1>（干净）；没有再用 <title>（去站点尾巴）。
    title = next((text.strip() for level, text in items if level == 1 and text.strip()), "")
    if not title:
        title = _strip_site_suffix(clean("".join(stripper.title_parts)))

    blocks: list[Block] = []
    # 标题占第 1 行（有标题时正文从第 2 行起），保持 line_from 单调
    line = 1 if title else 0
    for level, text in items:
        cleaned = clean(text)
        if not cleaned:
            continue
        if level:
            if title and cleaned == title:
                continue  # 正文里的同文 <h1> 由首块标题代表，避免重复
            line += 1
            blocks.append(
                Block(
                    text=cleaned,
                    content_type=TYPE_HEADING,
                    level=min(level, 4),
                    line_from=line,
                    line_to=line,
                )
            )
            continue
        lines = cleaned.split("\n")
        for block in _paragraph_blocks(lines, start_no=line + 1):
            if looks_like_article_meta(block.text):
                continue  # 日期/来源等文章元信息不是正文；同片段里的正文照常保留
            blocks.append(block)
        line += len(lines)

    if title:
        # 必须放在首块：导入层是按"首块是不是原生 H1"决定要不要用文件名兜底的
        blocks.insert(
            0,
            Block(
                text=title,
                content_type=TYPE_HEADING,
                level=1,
                doc_title_hint=True,
                line_from=1,
                line_to=1,
            ),
        )
    return Unit(blocks=blocks)


#: Markdown front matter：文件开头的 ``--- ... ---``（YAML）。只在**最开头**认。
_FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def _split_front_matter(source: str) -> tuple[dict[str, object], str]:
    """把 Markdown 开头的 YAML front matter 拆出来，正文保持**原行号**。

    为什么手动拆而不是靠插件：不装 mdit-py-plugins 时，``---`` 会被当成 setext
    标题，front matter 直接变成一个 H2（实测）。手动拆是**无条件生效**的兜底。

    参数：
        source: 整份 Markdown 原文。
    返回：
        ``(元数据 dict, 去掉 front matter 的正文)``；正文用等量空行替换，
        所以后面 token 的 ``map`` 行号仍是**原文件行号**。
    """
    match = _FRONT_MATTER.match(source)
    if not match:
        return {}, source
    meta = _md_front_matter(match.group(1))
    blank = "\n" * match.group(0).count("\n")
    return meta, blank + source[match.end():]


def _md_front_matter(raw: str) -> dict[str, object]:
    """解析 front matter 的 YAML（没装 PyYAML 时退化成 ``key: value``）。"""
    try:
        import yaml
    except ImportError:
        meta: dict[str, object] = {}
        for line in raw.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip():
                meta[key.strip()] = value.strip()
        return meta
    try:
        loaded = yaml.safe_load(raw) or {}
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _md_task_marker(lines: list[str], index: int) -> str:
    """从原始行里取任务列表标记 ``[ ]`` / ``[x]``（插件会把它从 inline 里拿掉）。"""
    if 0 <= index < len(lines):
        found = re.search(r"\[([ xX])\]", lines[index])
        if found:
            return "[x]" if found.group(1).lower() == "x" else "[ ]"
    return ""


def _strip_html(raw: str) -> str:
    """Markdown 里的 HTML 块：去标签成文本（不解析属性/脚本）。"""
    return clean(re.sub(r"<[^>]+>", "", raw))


def _md_parser():
    """构造 MarkdownIt（gfm-like）+ 可选插件；插件缺失时降级但不失败。"""
    from markdown_it import MarkdownIt

    parser = MarkdownIt("gfm-like", {"html": True, "linkify": False})
    try:
        from mdit_py_plugins.deflist import deflist_plugin
        from mdit_py_plugins.footnote import footnote_plugin
        from mdit_py_plugins.tasklists import tasklists_plugin
    except ImportError:
        return parser
    return parser.use(tasklists_plugin).use(footnote_plugin).use(deflist_plugin)


def _md_table_rows(tokens: list, start: int) -> tuple[list[str], int]:
    """扫 ``table_open..table_close``，按 ``tr`` 边界拼行；返回 ``(行文本, close 下标)``。"""
    rows: list[str] = []
    current_row: list[str] = []
    end = start
    while end < len(tokens) and tokens[end].type != "table_close":
        current = tokens[end]
        if current.type in ("tr_open", "thead_open", "tbody_open"):
            current_row = []
        elif current.type == "inline":
            current_row.append(str(current.content or "").strip())
        elif current.type in ("tr_close", "thead_close", "tbody_close"):
            if current_row:
                rows.append(" | ".join(current_row))
            current_row = []
        end += 1
    if current_row:
        # 容错：表格没正常闭合（截断的文件）时，最后一行也别丢
        rows.append(" | ".join(current_row))
    return rows, end


def _md_unit(path: Path) -> Unit:
    """Markdown：markdown-it-py 原生结构（标题/列表/引用/表格/代码/图片/元数据）。

    与旧实现的区别（旧版实测的 bug）：

    - **不再重复**：只按 ``inline`` 产一次，列表项/引用不会出现两遍；
    - **front matter 变成元数据**：``title`` 作文档标题（``doc_title_hint``），不进正文；
    - **列表保留结构**：每项一块，带 ``-`` / ``1.`` 标记与两空格缩进层级；
    - **引用**前缀 ``> ``；**图片**产出 ``TYPE_IMAGE`` 占位；**HTML 块**去标签。

    行号来自 token 的 ``map``（``[start, end)`` 0 基 → ``line_from = start + 1``）。

    参数：
        path: md 文件。
    返回：
        单个单元（块按文档顺序）。
    异常：
        ParseError: 没装 markdown-it-py。
    """
    try:
        from markdown_it import MarkdownIt  # noqa: F401 - 只做依赖探测
    except ImportError as exc:
        raise ParseError("解析 Markdown 需要 markdown-it-py（还没装）") from exc

    source = _read_text(path)
    meta, body = _split_front_matter(source)
    tokens = _md_parser().parse(body)
    source_lines = body.splitlines()

    blocks: list[Block] = []
    notes: list[str] = []
    image_count = 0
    #: 列表容器栈：``{"ordered": bool, "marker": str, "counter": int}``
    list_stack: list[dict[str, object]] = []
    #: 当前列表项的标记（``-`` / ``1.`` / ``- [ ]``），与嵌套层级对齐
    item_markers: list[str] = []
    quote_depth = 0

    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = token.type

        if kind == "heading_open":
            level = int(getattr(token, "tag", "1")[-1] or 1)
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            text = _md_inline(str(getattr(inline, "content", "") or ""))
            if text:
                blocks.append(
                    Block(
                        text=text,
                        content_type=TYPE_HEADING,
                        level=level,
                        line_from=int(token.map[0]) + 1 if token.map else None,
                        line_to=int(token.map[1]) if token.map else None,
                    )
                )
            index += 2  # 跳过 inline
            continue

        if kind == "table_open":
            rows, end = _md_table_rows(tokens, index)
            if rows:
                header = tuple(cell.strip() for cell in rows[0].split(" | ") if cell.strip()) if rows else ()
                blocks.append(
                    Block(
                        text="\n".join(rows),
                        content_type=TYPE_TABLE,
                        table_ref="md:#1",
                        row_from=1,
                        row_to=max(1, len(rows)),
                        col_from=1,
                        col_to=max(1, len(header)) if header else None,
                        header=header,
                        line_from=int(token.map[0]) + 1 if token.map else None,
                        line_to=int(token.map[1]) if token.map else None,
                    )
                )
            index = end + 1
            continue

        if kind in ("fence", "code_block"):
            content = str(getattr(token, "content", "") or "")
            start = getattr(token, "map", None)
            blocks.append(
                Block(
                    text=content,
                    content_type=TYPE_CODE,
                    line_from=int(start[0]) + 1 if start else None,
                    line_to=int(start[1]) if start else None,
                )
            )
            index += 1
            continue

        if kind == "html_block":
            text = _strip_html(str(getattr(token, "content", "") or ""))
            if text:
                blocks.append(
                    Block(
                        text=text,
                        line_from=int(token.map[0]) + 1 if token.map else None,
                        line_to=int(token.map[1]) if token.map else None,
                    )
                )
            index += 1
            continue

        if kind == "bullet_list_open":
            list_stack.append({"ordered": False, "marker": token.markup or "-", "counter": 0})
            index += 1
            continue
        if kind == "ordered_list_open":
            start = 1
            for key, value in (token.attrs or {}).items():
                if key == "start":
                    with contextlib.suppress(TypeError, ValueError):
                        start = int(value)
            list_stack.append({"ordered": True, "marker": token.markup or ".", "counter": start - 1})
            index += 1
            continue
        if kind in ("bullet_list_close", "ordered_list_close"):
            if list_stack:
                list_stack.pop()
            index += 1
            continue
        if kind == "list_item_open":
            marker = "-"
            if list_stack:
                current = list_stack[-1]
                if current["ordered"]:
                    current["counter"] = int(current["counter"]) + 1
                    marker = f"{current['counter']}{current['marker']}"
                else:
                    marker = str(current["marker"])
            if "task-list-item" in str((token.attrs or {}).get("class", "")):
                task = _md_task_marker(source_lines, int(token.map[0]) if token.map else -1)
                if task:
                    marker = f"{marker} {task}"
            item_markers.append(marker)
            index += 1
            continue
        if kind == "list_item_close":
            if item_markers:
                item_markers.pop()
            index += 1
            continue
        if kind == "blockquote_open":
            quote_depth += 1
            index += 1
            continue
        if kind == "blockquote_close":
            quote_depth = max(0, quote_depth - 1)
            index += 1
            continue

        if kind == "inline":
            line_from = int(token.map[0]) + 1 if token.map else None
            line_to = int(token.map[1]) if token.map else None
            for child in getattr(token, "children", None) or []:
                if child.type == "image":
                    image_count += 1
                    blocks.append(
                        Block(
                            text=str(getattr(child, "content", "") or "").strip(),
                            content_type=TYPE_IMAGE,
                            line_from=line_from,
                            line_to=line_to,
                        )
                    )
            text = _md_inline(str(token.content or "")).strip()
            if text and not looks_like_article_meta(text):
                prefix = "> " * quote_depth
                if item_markers:
                    prefix += "  " * max(0, len(list_stack) - 1)
                    prefix += f"{item_markers[-1]} "
                blocks.append(Block(text=prefix + text, line_from=line_from, line_to=line_to))
            index += 1
            continue

        index += 1

    title = str(meta.get("title") or "").strip()
    if title:
        duplicate = any(
            block.content_type == TYPE_HEADING and block.level == 1 and block.text == title
            for block in blocks
        )
        if not duplicate:
            # 首块 H1 + doc_title_hint：导入层据此把 front matter 标题记为 doc_title
            blocks.insert(
                0,
                Block(
                    text=title,
                    content_type=TYPE_HEADING,
                    level=1,
                    doc_title_hint=True,
                    line_from=1,
                    line_to=1,
                ),
            )
    if meta.get("date") or meta.get("tags"):
        bits = []
        if meta.get("date"):
            bits.append(f"日期 {meta['date']}")
        if meta.get("tags"):
            bits.append(f"标签 {meta['tags']}")
        notes.append("front matter：" + " · ".join(bits))
    if image_count:
        notes.append(f"文档含 {image_count} 张图片（Markdown 图片链接，暂未抓取内容）")
    return Unit(blocks=blocks, notes=notes)
