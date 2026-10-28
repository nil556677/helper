"""PDF 表格的辅助：可选结构适配器 + 表题注挂载（拆出以控制单文件行数）。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from app.core.parse.model import TYPE_TABLE, TYPE_TEXT


def structure_grid(path: Path, page: int, bbox: tuple[float, float, float, float]) -> Any:
    """可选表格结构后端（默认 off；失败静默回退 pdfplumber）。"""
    try:
        from app.integrations.table_structure import get_adapter

        adapter = get_adapter()
        if adapter is None:
            return None
        return adapter.extract(path, page, bbox)
    except Exception:
        return None


def attach_table_titles(blocks: list[object]) -> list[object]:
    """把表格正上方最近的“表 X …”短文本挂成 `table_title`（尽力而为）。"""
    for index, block in enumerate(blocks):
        if getattr(block, "content_type", "") != TYPE_TABLE or not getattr(block, "coords", None):
            continue
        table_y0 = float(block.coords[1])
        best = None
        best_gap = 1e9
        for candidate in blocks:
            if getattr(candidate, "content_type", "") != TYPE_TEXT:
                continue
            if getattr(candidate, "page", None) != getattr(block, "page", None):
                continue
            coords = getattr(candidate, "coords", None)
            if not coords:
                continue
            text = str(getattr(candidate, "text", "") or "").strip()
            if not text or len(text) > 120:
                continue
            if not text.startswith(("表 ", "表1", "表2", "表3", "Table ")):
                continue
            gap = table_y0 - float(coords[3])
            if 0 <= gap <= 90 and gap < best_gap:
                best, best_gap = text, gap
        if best:
            blocks[index] = replace(block, table_title=best)
    return blocks
