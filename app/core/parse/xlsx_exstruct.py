"""可选后端：ExStruct 结构化 Excel 提取（**默认关闭，失败自动回退 openpyxl**）。

为什么要单独成一个模块：openpyxl 路径（:mod:`app.core.parse.xlsx`）对主流程够用且稳定，
但**繁琐的 OOXML 构造**（多层合并/隐藏 sheet/图表/形状/复杂表格）它处理不了。
ExStruct 能在 ``light`` 模式下直接解析 OOXML、给 ``table_candidates`` 与合并单元格，
更适合这类文件。但它不是本仓库默认依赖，也不必须在所有机器上装。

激活条件（两个都满足才走它）：
1. 环境变量 ``HELPER_XLSX_BACKEND=exstruct``；
2. ``exstruct`` 可导入。

**失败即回退**：本模块内部任何异常都向上抛，由 :func:`app.core.parse.xlsx.iter_units`
catch 后回到 openpyxl——可选后端绝不挡主流程。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from app.core.parse.model import TYPE_HEADING, TYPE_TABLE, Block, Unit


def available() -> bool:
    """是否启用 ExStruct 后端（环境开关 + 包已安装）。"""
    if os.environ.get("HELPER_XLSX_BACKEND", "").strip().lower() != "exstruct":
        return False
    try:
        import exstruct  # noqa: F401

        return True
    except Exception:
        return False


def _workbook_data(path: Path) -> dict:
    """用 ExStruct 提取并序列化工作簿（``to_json`` 是库文档承诺的稳定口径）。"""
    from exstruct import extract

    wb_obj = extract(str(path), mode="light")
    if hasattr(wb_obj, "to_json"):
        data = wb_obj.to_json()
        if isinstance(data, dict):
            return data
    # 部分版本返回 WorkbookData 而非 dict：S 化回退。
    if hasattr(wb_obj, "sheets"):
        result: dict = {"sheets": {}}
        sheets = wb_obj.sheets
        if isinstance(sheets, dict):
            for name, sheet in sheets.items():
                result["sheets"][name] = (
                    sheet.to_json() if hasattr(sheet, "to_json") else sheet
                )
        return result
    # 兜底：当作已序列化的 dict
    return dict(wb_obj)  # type: ignore[arg-type, call-overload]


def _grid(sheet: dict) -> list[tuple[int, list[str]]]:
    """把 ``rows``（``[{"r":行号, "c":{列号:值}}]``）转成按最大列补齐的网格。"""
    rows_raw = sheet.get("rows") or []
    by_row: dict[int, dict[int, str]] = {}
    max_col = 0
    for item in rows_raw:
        r = int(item.get("r"))
        cols = item.get("c") or {}
        cell: dict[int, str] = {}
        for col, value in cols.items():
            c = int(col)
            cell[c] = str(value) if value is not None else ""
            max_col = max(max_col, c)
        if cell:
            by_row[r] = cell
    if max_col == 0:
        return []
    grid: list[tuple[int, list[str]]] = []
    for r in sorted(by_row):
        cells = [by_row[r].get(c, "") for c in range(1, max_col + 1)]
        grid.append((r, cells))
    return grid


def iter_units(path: Path) -> Iterator[Unit]:
    """走 ExStruct 产出的表格单元；任何异常向上抛（调用方负责回退）。"""
    data = _workbook_data(path)
    sheets = data.get("sheets") or {}
    if not isinstance(sheets, dict):
        sheets = {}
    for name, sheet in sheets.items():
        if not isinstance(sheet, dict):
            continue
        # 隐藏 sheet：ExStruct light 仍会给；这里跳过，避免把用户没看到的表收进来。
        if bool(sheet.get("visible")) is False or str(sheet.get("hidden", "")).lower() == "true":
            continue
        grid = _grid(sheet)
        if not grid:
            continue
        # 空行切段（与 openpyxl 路径同口径：空行 = 上下多张表）
        segments: list[list[tuple[int, list[str]]]] = []
        current: list[tuple[int, list[str]]] = []
        for row_no, cells in grid:
            if any(cells):
                current.append((row_no, cells))
            elif current:
                segments.append(current)
                current = []
        if current:
            segments.append(current)
        if not segments:
            continue
        yield Unit(blocks=[Block(text=str(name), content_type=TYPE_HEADING, level=1)])
        for ordinal, segment in enumerate(segments, start=1):
            header = segment[0][1]
            text = "\n".join(
                [" | ".join(header), *[" | ".join(c) for _r, c in segment[1:]]]
            )
            yield Unit(
                blocks=[
                    Block(
                        text=text,
                        content_type=TYPE_TABLE,
                        table_ref=f"sheet:{name}#{ordinal}",
                        row_from=segment[0][0],
                        row_to=segment[-1][0],
                        col_from=1,
                        col_to=max(1, len(header)),
                        header=tuple(header),
                    )
                ]
            )
