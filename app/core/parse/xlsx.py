"""Excel（.xlsx/.xlsm）：结构化表格解析（合并单元格 / 空列 / 表头 / 行列号）。

## 与旧实现的区别

旧实现把每个工作表按空行切成"表格段"，然后 ``" | ".join(cells)``——但：

1. **空单元格被直接丢掉**，空列会把后面所有列挤左移，表格结构损坏；
2. ``read_only=True`` 拿不到合并单元格，合并表头/合并值丢失；
3. 数字 ``str(cell)`` 变成 ``3.0``、日期变成 datetime 的 Python 表示；
4. 没有表头/列号/行号元数据，引用无法定位。

本模块改为：

- **普通大小文件 ``read_only=False + data_only=True``**（能读 ``merged_cells`` 且公式取缓存值）；
  超大文件（>15 MB）退回 ``read_only=True``（放弃合并单元格，速度优先）；
- 每行按**最大列数**补齐，空单元格保留为 ``""``，列不错位；
- **合并单元格展开**：区域内所有格填左上角的值，表头不再缺列；
- 数字/日期按显示格式归一（``3.0``→``3``、日期→``YYYY-MM-DD``）；
- 每个表格段产出 ``TYPE_TABLE`` 块，带 ``table_ref``、绝对行号
  （``row_from/row_to``）、列号（``col_from/col_to``）、表头（``header``）；
  文本首行是表头，后续行是数据——切分器的原子表格路径会按整行切、每段重复表头。
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from pathlib import Path

from app.core.parse.model import TYPE_HEADING, TYPE_TABLE, Block, ParseError, Unit

#: 超过这个大小（字节）就退回 read_only：合并单元格信息丢失，但内存可控。
_READ_ONLY_THRESHOLD = 15_000_000


def _cell_text(value: object) -> str:
    """把单元格值归一成显示文本（数字去掉 ``.0``、日期格式化、空值→空串）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)):
        try:
            if value == int(value):  # type: ignore[comparison-overlap]
                return str(int(value))
        except (TypeError, ValueError, OverflowError):
            pass
        return str(value)
    text = str(value).strip()
    return text


def _grid(sheet: object, max_col: int, read_only: bool) -> dict[int, list[str]]:
    """把 sheet 读成 ``{绝对行号: [单元格文本×max_col]}``（空列/空行都保留）。

    空行**必须保留**：它是"上下多张表"的段边界（见 :func:`iter_units`）。
    """
    rows: dict[int, list[str]] = {}
    if read_only:
        for row_no, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            cells = [_cell_text(cell) for cell in row[:max_col]]
            cells.extend([""] * (max_col - len(cells)))
            rows[row_no] = cells
        return rows

    for row_no, row in enumerate(
        sheet.iter_rows(min_row=1, max_row=sheet.max_row, max_col=max_col), start=1
    ):
        cells = [_cell_text(cell.value) for cell in row[:max_col]]
        cells.extend([""] * (max_col - len(cells)))
        rows[row_no] = cells
    return rows


def _expand_merged(sheet: object, rows: dict[int, list[str]], max_col: int) -> None:
    """把合并单元格区域内所有格填成左上角的值（read_only 模式没有合并信息，直接跳过）。"""
    merged = getattr(sheet, "merged_cells", None)
    ranges = getattr(merged, "ranges", None) if merged is not None else None
    if not ranges:
        return
    for span in ranges:
        try:
            top_row, top_col = int(span.min_row), int(span.min_col)
            value = rows.get(top_row, [""] * max_col)[top_col - 1]
        except (AttributeError, IndexError, TypeError):
            continue
        if not value:
            continue
        for row_no in range(int(span.min_row), int(span.max_row) + 1):
            target = rows.get(row_no)
            if target is None:
                continue
            for col_no in range(int(span.min_col), int(span.max_col) + 1):
                if 1 <= col_no <= len(target):
                    target[col_no - 1] = value


def _comment_text(comment: object) -> str:
    """把 Excel 批注正文清洗成一句（去掉 ``作者:`` 行、合并换行）。"""
    raw = str(getattr(comment, "text", "") or "").strip()
    if not raw:
        return ""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if lines and (
        lines[0].startswith("作者:")
        or lines[0].startswith("作者：")
        or lines[0].lower().startswith("author:")
    ):
        lines = lines[1:]
    return " ".join(lines).strip()


def _cell_comments(sheet: object) -> dict[tuple[int, int], str]:
    """``{(行, 列): 批注正文}``；只有非 read_only 模式拿得到批注。"""
    out: dict[tuple[int, int], str] = {}
    try:
        for row in sheet.iter_rows():  # type: ignore[attr-defined]
            for cell in row:
                comment = getattr(cell, "comment", None)
                if comment is None:
                    continue
                text = _comment_text(comment)
                if text:
                    out[(int(cell.row), int(cell.column))] = text
    except Exception:
        return {}
    return out


def _header_row_count(sheet: object, rows: dict[int, list[str]]) -> int:
    """判断表头占几行：首两行里有合并单元格 → 视为多级表头（2 行）。

    只在**前两行**内判断，避免把数据区的合并单元格当成表头。read_only 模式没有
    merged 信息 → 永远按 1 行处理（保守）。
    """
    if 2 not in rows or not any(rows.get(2, ())):
        return 1
    merged = getattr(sheet, "merged_cells", None)
    ranges = getattr(merged, "ranges", None) if merged is not None else None
    if not ranges:
        return 1
    for span in ranges:
        try:
            if int(span.min_row) == 1 and int(span.max_row) <= 2:
                return 2
        except (AttributeError, TypeError, ValueError):
            continue
    return 1


def _flatten_header(
    header_rows: list[list[str]], max_col: int
) -> list[str]:
    """把多行表头按列拼成 ``一级/二级`` 叶子列名；空列给 ``列N``，重名加序号。"""
    names: list[str] = []
    for col in range(max_col):
        path: list[str] = []
        for cells in header_rows:
            value = cells[col] if col < len(cells) else ""
            if value and (not path or path[-1] != value):
                path.append(value)
        names.append("/".join(path) or f"列{col + 1}")
    # 同名列去重：后端按列名序列化，重名会让 "成本中心: x" 分不清是哪一列
    counts: dict[str, int] = {}
    unique: list[str] = []
    for name in names:
        counts[name] = counts.get(name, 0) + 1
        unique.append(name if counts[name] == 1 else f"{name}({counts[name]})")
    return unique


def iter_units(path: Path) -> Iterator[Unit]:
    """按"空行切出的表格段"逐段产出（结构化、保留空列/合并/表头/行列号）。

    优先走可选后端 ExStruct（装了且开开关）；任何异常都回退到 openpyxl 主路径。
    """
    from app.core.parse import xlsx_exstruct

    if xlsx_exstruct.available():
        try:
            return xlsx_exstruct.iter_units(path)
        except Exception:
            pass
    return _iter_openpyxl(path)


def _iter_openpyxl(path: Path) -> Iterator[Unit]:
    """openpyxl 结构化表格主路径（稳定、默认）。"""
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ParseError("解析 Excel 需要 openpyxl（还没装）") from exc

    try:
        read_only = path.stat().st_size > _READ_ONLY_THRESHOLD
        workbook = openpyxl.load_workbook(path, read_only=read_only, data_only=True)
    except Exception as exc:
        raise ParseError(f"打不开这份 Excel（可能损坏）：{exc}") from exc

    try:
        prepared: list[tuple[object, int, dict[int, list[str]]]] = []
        for sheet in workbook.worksheets:
            try:
                max_col = int(getattr(sheet, "max_column", 0) or 0)
            except Exception:
                max_col = 0
            if max_col <= 0:
                continue
            rows = _grid(sheet, max_col, read_only)
            _expand_merged(sheet, rows, max_col)
            if not read_only:
                # 单元格批注：拼到该格值后面（F4/L24 这类注释不能丢）
                for (row_no, col_no), note in _cell_comments(sheet).items():
                    target = rows.get(row_no)
                    if target is None or not 1 <= col_no <= len(target):
                        continue
                    old = target[col_no - 1]
                    target[col_no - 1] = (
                        f"{old}（{note}）" if old else f"（{note}）"
                    )
            if rows:
                prepared.append((sheet, max_col, rows))
        if not prepared:
            return

        # ---- 注释页 ----
        # 主数据 sheet = 非空行最多的那张；其它“小、窄、非空”的 sheet 当注释页。
        # 注释页常见形态 ``代码 | 说明``（或 ``列名 | 说明``）：把说明拼到主表对应值后面。
        main_title = max(prepared, key=lambda item: len(item[2]))[0].title  # type: ignore[attr-defined]
        note_titles: set[str] = set()
        note_map: dict[str, str] = {}
        for sheet, max_col, rows in prepared:
            title = str(sheet.title)  # type: ignore[attr-defined]
            if title == main_title:
                continue
            hinted = any(word in title for word in ("注释", "说明", "字典", "映射", "Lookup"))
            if max_col <= 4 and len(rows) <= 80:
                for row in rows.values():
                    cells = [cell for cell in row if cell]
                    if len(cells) >= 2 and cells[0] not in note_map:
                        note_map[cells[0]] = cells[1]
                if hinted or note_map:
                    note_titles.add(title)
        if note_map:
            for sheet, _max_col, rows in prepared:
                if str(sheet.title) in note_titles:  # type: ignore[attr-defined]
                    continue
                for row in rows.values():
                    for index, cell in enumerate(row):
                        note = note_map.get(cell)
                        if note and "（" not in cell:
                            row[index] = f"{cell}（{note}）"

        for sheet, max_col, rows in prepared:
            if str(sheet.title) in note_titles:  # type: ignore[attr-defined]
                continue

            # 空行切段（同一 sheet 上下多张表）；行按绝对行号排序
            ordered = sorted(rows.items())
            segments: list[list[tuple[int, list[str]]]] = []
            current: list[tuple[int, list[str]]] = []
            for row_no, cells in ordered:
                if any(cells):
                    current.append((row_no, cells))
                elif current:
                    segments.append(current)
                    current = []
            if current:
                segments.append(current)
            if not segments:
                continue

            # sheet 名当标题块
            yield Unit(
                blocks=[Block(text=str(sheet.title), content_type=TYPE_HEADING, level=1)]
            )
            header_rows = _header_row_count(sheet, rows)
            #: 一个 sheet 的表头只认**第一段的开头**；空行之后继续的数据段沿用同一个表头。
            #: 不能每段都把首行当表头——否则"表中断开的数据段"会把第一条数据当列名。
            sheet_header: list[str] | None = None
            for ordinal, segment in enumerate(segments, start=1):
                if sheet_header is None:
                    header_grid = [cells for _row_no, cells in segment[:header_rows]]
                    sheet_header = _flatten_header(header_grid, max_col)
                    data_rows = segment[header_rows:]
                else:
                    data_rows = segment
                header = sheet_header
                if not data_rows:
                    continue
                # **child 文本只放行级 ``列名: 值``**，不再把裸表头行塞进每个片段。
                # 裸表头 + 只保留非空列的值会导致"表头有 40 列、数据只有 20 列"的错位；
                # 列名已经写在每个值前面，表头完整信息放 summary 和 locator.header。
                row_lines: list[str] = []
                for _row_no, cells in data_rows:
                    parts = [
                        f"{header[index] or f'列{index + 1}'}: {cells[index]}"
                        for index in range(min(len(header), len(cells)))
                        if cells[index]
                    ]
                    if parts:
                        row_lines.append(" | ".join(parts))
                if not row_lines:
                    continue
                text = "\n".join(row_lines)
                yield Unit(
                    blocks=[
                        Block(
                            text=text,
                            content_type=TYPE_TABLE,
                            table_ref=f"sheet:{sheet.title}#{ordinal}",
                            row_from=data_rows[0][0],
                            row_to=segment[-1][0],
                            col_from=1,
                            col_to=max_col,
                            header=tuple(header),
                        )
                    ]
                )
    finally:
        workbook.close()
