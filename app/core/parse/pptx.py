"""PowerPoint（.pptx）：每页的文本框与表格 → 带页码的块。

页码 = 幻灯片序号（引用"第 N 页"对 PPT 就是第 N 张幻灯片）。
表格块：``slide:N#序号``，整块保留。
"""

from __future__ import annotations

import io

from app.core.parse.model import TYPE_TABLE, TYPE_TEXT, Block, ParsedDocument, ParseError


def parse(data: bytes, _name: str) -> ParsedDocument:
    try:
        from pptx import Presentation  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ParseError("解析 PPT 需要 python-pptx（还没装）") from exc

    try:
        presentation = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise ParseError(f"打不开这份 PPT（可能损坏）：{exc}") from exc

    blocks: list[Block] = []
    for slide_index, slide in enumerate(presentation.slides, start=1):
        table_no = 0
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                table_no += 1
                rows = [
                    [cell.text.strip() for cell in row.cells]
                    for row in shape.table.rows
                ]
                rendered = "\n".join(" | ".join(cell for cell in row if cell) for row in rows if any(row))
                blocks.append(
                    Block(
                        text=rendered,
                        content_type=TYPE_TABLE,
                        page=slide_index,
                        table_ref=f"slide:{slide_index}#{table_no}",
                        row_from=1,
                        row_to=max(1, len(rows)),
                    )
                )
            elif getattr(shape, "has_text_frame", False):
                text = str(shape.text_frame.text or "").strip()
                if text:
                    blocks.append(Block(text=text, content_type=TYPE_TEXT, page=slide_index))

    return ParsedDocument(
        blocks=blocks,
        pages=len(presentation.slides),
        table_count=sum(1 for block in blocks if block.content_type == TYPE_TABLE),
    )
