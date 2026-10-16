"""Excel（.xlsx/.xlsm）：**每个工作表按空行切出多个表格块**（P1.1 的"多个表格"问题）。

为什么按空行切：一个 sheet 里可能横着放好几张表（中间用空行隔开）。整张 sheet 并成一块，
引用就说不清"是哪张表"；按空行切成多块后，每块是 ``sheet:名称#序号`` 且带**绝对行号**
（openpyxl 的行号，跳转重读时直接定位）。

精度细节：
- ``read_only=True, data_only=True``：只读流式（大表不爆内存）、公式取**缓存值**；
- 空行 = 整行所有单元格都没有值；
- 行号是 sheet 绝对行号（1 基），跨块不重置。
"""

from __future__ import annotations

import io

from app.core.parse.model import TYPE_TABLE, Block, ParsedDocument, ParseError


def parse(data: bytes, _name: str) -> ParsedDocument:
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ParseError("解析 Excel 需要 openpyxl（还没装）") from exc

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ParseError(f"打不开这份 Excel（可能损坏）：{exc}") from exc

    blocks: list[Block] = []
    try:
        for sheet in workbook.worksheets:
            # 逐行收集（含行号），按空行切成段
            segments: list[list[tuple[int, list[str]]]] = []
            current: list[tuple[int, list[str]]] = []
            for row_no, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                if cells:
                    current.append((row_no, cells))
                elif current:
                    segments.append(current)
                    current = []
            if current:
                segments.append(current)

            for ordinal, segment in enumerate(segments, start=1):
                rows = [" | ".join(cells) for _row_no, cells in segment]
                first_row = segment[0][0]
                last_row = segment[-1][0]
                blocks.append(
                    Block(
                        text="\n".join(rows),
                        content_type=TYPE_TABLE,
                        table_ref=f"sheet:{sheet.title}#{ordinal}",
                        row_from=first_row,
                        row_to=last_row,
                    )
                )
    finally:
        workbook.close()

    return ParsedDocument(
        blocks=blocks,
        table_count=sum(1 for block in blocks if block.content_type == TYPE_TABLE),
    )
