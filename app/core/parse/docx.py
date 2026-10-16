"""Word（.docx）：**按 body 原始顺序**遍历段落与表格（P1.1 定稿的细节）。

**为什么必须走 ``document.element.body`` 而不是 ``document.paragraphs``**：
python-docx 的 ``paragraphs`` 和 ``tables`` 是两条**独立迭代器**，混排文档的原始交错顺序
会丢（段落、表格各收一摞）——表格块就放不回正文顺序里了。这里按 XML 子节点逐个判断
``w:p``（段落）还是 ``w:tbl``（表格），顺序即原文档顺序。

**结构（P1.2 的原料，这里是原生的）**：
- ``paragraph.style.name`` 是 Word 自己存的样式（"Heading 1"/"标题 1"）——比字号启发式准；
- ``outlineLevel`` 是更底层的原生层级；两者都没有才轮到字号/加粗（留给 P1.2 的兜底）；
- 每个块带 ``para_index``（body 顺序下标，0 基）——引用"第 N 段"的定位锚点。
"""

from __future__ import annotations

import io
import re

from docx.oxml.ns import qn

from app.core.parse.model import (
    TYPE_HEADING,
    TYPE_TABLE,
    TYPE_TEXT,
    Block,
    ParsedDocument,
    ParseError,
)

HEADING_STYLE = re.compile(r"^(?:heading|标题)\s*([1-9])", re.IGNORECASE)



def _paragraph_level(paragraph) -> tuple[int | None, str]:
    """取段落层级：优先 outlineLevel，其次样式名（Heading N / 标题 N）。"""
    p_pr = paragraph._p.pPr
    if p_pr is not None and p_pr.outlineLvl is not None:
        level = p_pr.outlineLvl.val
        if level is not None and 0 <= int(level) <= 8:
            return int(level) + 1, str(paragraph.style.name if paragraph.style else "")
    style_name = str(paragraph.style.name) if paragraph.style else ""
    match = HEADING_STYLE.match(style_name)
    if match:
        return int(match.group(1)), style_name
    return None, style_name


def _run_style(paragraph) -> tuple[float | None, bool]:
    """段落字号/加粗（P1.2 启发式兜底的原料；显式设置才有，样式继承的为 None）。"""
    size: float | None = None
    bold = False
    for run in paragraph.runs:
        if run.font.size is not None:
            size = float(run.font.size.pt)
        if run.bold:
            bold = True
    return size, bold


def _walk_body(document) -> list[object]:
    """按 body 顺序产出 ``(is_paragraph, obj)`` 序列。"""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    items: list[object] = []
    for child in document.element.body:
        if child.tag == qn("w:p"):
            items.append(Paragraph(child, document))
        elif child.tag == qn("w:tbl"):
            items.append(Table(child, document))
    return items


def _table_blocks(table, element_index: int, table_no: int) -> Block:
    rows = [
        [cell.text.strip() for cell in row.cells]
        for row in table.rows
    ]
    rendered = "\n".join(" | ".join(cell for cell in row if cell) for row in rows if any(row))
    return Block(
        text=rendered,
        content_type=TYPE_TABLE,
        table_ref=f"tbl#{table_no}",
        row_from=1,
        row_to=max(1, len(rows)),
        para_index=element_index,
    )


def parse(data: bytes, _name: str) -> ParsedDocument:
    try:
        import docx  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ParseError("解析 Word 需要 python-docx（还没装）") from exc
    from docx.table import Table

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ParseError(f"打不开这份 Word 文档（可能损坏）：{exc}") from exc

    blocks: list[Block] = []
    table_no = 0
    for element_index, element in enumerate(_walk_body(document)):
        # 表格
        if isinstance(element, Table):
            table_no += 1
            blocks.append(_table_blocks(element, element_index, table_no))
            continue
        # 段落
        paragraph = element
        text = paragraph.text.strip()
        if not text:
            continue
        level, _style = _paragraph_level(paragraph)
        size, bold = _run_style(paragraph)
        if level is not None:
            blocks.append(
                Block(
                    text=text,
                    content_type=TYPE_HEADING,
                    level=level,
                    font_size=size,
                    bold=bold,
                    para_index=element_index,
                )
            )
        else:
            blocks.append(
                Block(
                    text=text,
                    content_type=TYPE_TEXT,
                    font_size=size,
                    bold=bold,
                    para_index=element_index,
                )
            )

    return ParsedDocument(
        blocks=blocks,
        table_count=sum(1 for block in blocks if block.content_type == TYPE_TABLE),
    )
