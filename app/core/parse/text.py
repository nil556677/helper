"""纯文本与标记类：txt / md / csv / tsv / json / html → 块。

**行号是这类格式的定位锚点**：txt/md 的块记录**文件行号**（md 用 markdown-it-py 的
``token.map``，原生带行号；txt 直接数行），这样引用能说"第 12–20 行"。
csv/tsv 是**一张表**（表格整块保留，行区间进块）。
"""

from __future__ import annotations

import csv
import io
import json
import re
from html.parser import HTMLParser
from typing import ClassVar

from app.core.parse.model import (
    TYPE_CODE,
    TYPE_TABLE,
    Block,
    ParsedDocument,
    ParseError,
)

#: 纯文本类后缀（不需要第三方库）
TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json", ".html", ".htm"})


class _TagStripper(HTMLParser):
    """把 HTML 变成纯文本（丢掉 script/style，块级标签换成换行）。"""

    _BLOCK: ClassVar[set[str]] = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    _SKIP: ClassVar[set[str]] = {"script", "style", "noscript", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs  # HTMLParser 回调签名要求该参数；我们只看 tag
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def decode(data: bytes) -> str:
    """解码：UTF-8(含 BOM) → GBK → 兜底替换。

    为什么要有 GBK：国内机器上"另存为 ANSI"很常见，只按 UTF-8 读会直接抛解码错。
    """
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def clean(text: str) -> str:
    """压掉多余空白：连续空行变一个，行尾空格去掉。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _paragraph_blocks(text: str) -> list[Block]:
    """按空行分组一段文本（txt/log/html 用），块带**文件行号**。"""
    lines = clean(text).split("\n")
    blocks: list[Block] = []
    current: list[str] = []
    start: int | None = None
    for index, line in enumerate(lines, start=1):
        if line.strip():
            if start is None:
                start = index
            current.append(line)
        elif current:
            blocks.append(
                Block(text="\n".join(current).strip(), line_from=start, line_to=index - 1)
            )
            current, start = [], None
    if current:
        blocks.append(Block(text="\n".join(current).strip(), line_from=start, line_to=len(lines)))
    return blocks


def _parse_text(data: bytes, _name: str) -> ParsedDocument:
    text = decode(data)
    if _name.lower().endswith((".html", ".htm")):
        stripper = _TagStripper()
        stripper.feed(text)
        text = "".join(stripper.parts)
        # HTML 抽出来的行号和源码行号对不上，不给定位（诚实：宁可不定位也不错位）
        return ParsedDocument(blocks=_paragraph_blocks(text))
    return ParsedDocument(blocks=_paragraph_blocks(text))


def _parse_csv(data: bytes, name: str) -> ParsedDocument:
    """CSV/TSV：整份就是**一张表**，表格整块保留，行区间进块。"""
    text = decode(data)
    delimiter = "\t" if name.lower().endswith(".tsv") else ","
    rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        cells = [cell.strip() for cell in row if cell and cell.strip()]
        if cells:
            rows.append(cells)
    if not rows:
        return ParsedDocument(blocks=[])
    rendered = "\n".join(" | ".join(row) for row in rows)
    return ParsedDocument(
        blocks=[
            Block(
                text=rendered,
                content_type=TYPE_TABLE,
                table_ref=f"csv:{name}#1",
                row_from=1,
                row_to=len(rows),
            )
        ]
    )


def _parse_json(data: bytes, _name: str) -> ParsedDocument:
    """JSON：拍平成"键: 值"的行（没有文件行号可言，不给定位）。"""
    try:
        payload = json.loads(decode(data))
    except ValueError as exc:
        raise ParseError(f"JSON 格式不对：{exc}") from exc
    lines: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        else:
            lines.append(f"{path}: {node}" if path else str(node))

    walk(payload, "")
    return ParsedDocument(blocks=[Block(text=clean("\n".join(lines)))])


def _parse_markdown(data: bytes, _name: str) -> ParsedDocument:
    """Markdown：用 markdown-it-py 的 token 树（**原生结构**）。

    - ``#``~``####`` → heading 块（level 直接是 token 层级，零启发式）；
    - 表格 → 整块保留（``table_open``..``table_close`` 跨度内还原成 " | " 网格）；
    - 代码块 → ``code`` 块（技术方案 §6 的 content_type 有它）；
    - 段落/列表项/引用 → text 块。
    **每个块都带文件行号**（``token.map`` 是 markdown-it 的块级行区间）。
    """
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:  # 依赖缺失时给人话
        raise ParseError("解析 Markdown 需要 markdown-it-py（还没装）") from exc

    text = decode(data)
    # gfm-like：commonmark **不含表格**（第一次实测 kinds 里没有 table），
    # 我们的场景（合同/说明类 md）表格是常客，所以要带表格的那套规则。
    # linkify 关掉：它要 linkify-it-py，而本环境没装、也用不上。
    tokens = MarkdownIt("gfm-like", {"html": True, "linkify": False}).parse(text)
    blocks: list[Block] = []

    def block_for(token: object, kind: str, level: int | None = None) -> Block:
        """按 token 建块（行号来自 map；inline token 没有 map，找外层块 token）。"""
        start = getattr(token, "map", None)
        content = getattr(token, "content", "") or ""
        if start is None:
            return Block(text=content, content_type=kind, level=level)
        return Block(
            text=content,
            content_type=kind,
            level=level,
            line_from=int(start[0]) + 1,
            line_to=int(start[1]),
        )

    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == "heading_open":
            level = int(getattr(token, "tag", "1")[-1] or 1)
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            content = getattr(inline, "content", "") if inline else ""
            blocks.append(
                Block(
                    text=content,
                    content_type="heading",
                    level=level,
                    line_from=int(token.map[0]) + 1,
                    line_to=int(token.map[1]),
                )
            )
            index += 2  # heading_open + inline（heading_close 无 map，跳过也行）
            continue
        if kind == "table_open":
            # 表格是平铺 token（table_open..table_close 是一串兄弟节点），整段收集
            rows: list[str] = []
            current_row: list[str] = []
            end = index
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
                rows.append(" | ".join(current_row))
            if rows:
                blocks.append(
                    Block(
                        text="\n".join(rows),
                        content_type=TYPE_TABLE,
                        table_ref="md:#1",
                        row_from=1,
                        row_to=max(1, len(rows)),
                        line_from=int(token.map[0]) + 1,
                        line_to=int(token.map[1]),
                    )
                )
            index = end + 1  # 跳过到 table_close 之后
            continue
        if kind in ("fence", "code_block"):
            blocks.append(block_for(token, TYPE_CODE))
        elif kind in ("paragraph_open", "blockquote_open", "list_item_open"):
            content = ""
            line_from = line_to = None
            if token.map:
                line_from, line_to = int(token.map[0]) + 1, int(token.map[1])
            for following in tokens[index + 1 : index + 4]:
                if following.type == "inline":
                    content = str(following.content or "")
                    break
            if content.strip():
                blocks.append(Block(text=content, line_from=line_from, line_to=line_to))
        index += 1

    return ParsedDocument(blocks=blocks)


def parse(data: bytes, name: str) -> ParsedDocument:
    """纯文本类入口（按后缀分派；异常给用户看）。"""
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix in (".csv", ".tsv"):
        return _parse_csv(data, name)
    if suffix == ".json":
        return _parse_json(data, name)
    if suffix in (".md", ".markdown"):
        return _parse_markdown(data, name)
    return _parse_text(data, name)
