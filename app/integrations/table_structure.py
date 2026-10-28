"""表格结构识别适配器（可选）。

设计目标：
- 默认关闭，不改变现有 pdfplumber 行为；
- 统一输出 `TableGrid`（单元格矩阵 + 合并范围 + 标题），供 PDF 解析器转成 Block；
- Docling（合并单元格/复杂版式）→ PaddleOCR（扫描/中文）→ 云（显式开启）。
- Camelot 后端已移除（依赖 pandas/opencv，安装成本高；文本层 PDF 继续用 pdfplumber）。

开关：环境变量 ``HELPER_TABLE_STRUCTURE`` = ``off`` / ``docling`` / ``paddle`` / ``cloud``。
重依赖全部惰性导入；不可用/失败一律返回 None，让解析器回退。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class TableGrid:
    """与具体库无关的表格网格（1 基合并范围，含端点）。"""

    cells: tuple[tuple[str, ...], ...]
    header_rows: int = 1
    merged_ranges: tuple[tuple[int, int, int, int], ...] = ()
    title: str = ""
    confidence: float = 0.0
    source: str = ""

    @property
    def width(self) -> int:
        return max((len(row) for row in self.cells), default=0)

    def to_block_parts(self) -> tuple[str, tuple[str, ...], tuple[tuple[int, int, int, int], ...]]:
        """转成解析器需要的 ``(text, header, merged_ranges)``。"""
        width = self.width
        rows = [tuple(list(row) + [""] * (width - len(row))) for row in self.cells]
        text = "\n".join(" | ".join(row) for row in rows)
        header = rows[0] if rows else ()
        return text, header, self.merged_ranges


class TableStructureAdapter(Protocol):
    name: str

    def available(self) -> bool: ...

    def extract(
        self,
        path: Any,
        page: int,
        bbox: tuple[float, float, float, float] | None = None,
        *,
        title: str = "",
    ) -> TableGrid | None: ...


def _grid_from_rows(
    rows: list[list[str]],
    *,
    title: str = "",
    merged: tuple[tuple[int, int, int, int], ...] = (),
    source: str = "",
    confidence: float = 0.5,
) -> TableGrid | None:
    """二维文本 → TableGrid（去尾部空列/空行，保留表头）。"""
    cleaned = [[str(cell or "").strip() for cell in row] for row in rows]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return None
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    return TableGrid(
        cells=tuple(tuple(row) for row in cleaned),
        merged_ranges=merged,
        title=title,
        confidence=confidence,
        source=source,
    )


class DoclingAdapter:
    """Docling：PDF/HTML/Office 的表格结构（含合并单元格），惰性、整份缓存。"""

    name = "docling"

    def __init__(self) -> None:
        self._documents: dict[str, Any] = {}

    def available(self) -> bool:
        try:
            import docling  # noqa: F401
        except Exception:
            return False
        return True

    def _document(self, path: Any) -> Any | None:
        key = str(path)
        if key in self._documents:
            return self._documents[key]
        try:
            from docling.document_converter import DocumentConverter

            doc = DocumentConverter().convert(key).document
        except Exception:
            doc = None
        self._documents[key] = doc
        return doc

    def extract(
        self,
        path: Any,
        page: int,
        bbox: tuple[float, float, float, float] | None = None,
        *,
        title: str = "",
    ) -> TableGrid | None:
        del bbox
        doc = self._document(path)
        if doc is None:
            return None
        try:
            tables = list(getattr(doc, "tables", ()) or ())
        except Exception:
            return None
        for table in tables:
            try:
                prov = (getattr(table, "prov", ()) or ())[0]
                table_page = int(getattr(prov, "page_no", 0) or 0)
            except Exception:
                table_page = 0
            if table_page and int(page) and table_page != int(page):
                continue
            grid = _grid_from_docling(table, title=title)
            if grid is not None:
                return grid
        return None


def _grid_from_docling(table: Any, *, title: str) -> TableGrid | None:
    try:
        data = table.data
        num_rows = int(getattr(data, "num_rows", 0) or 0)
        num_cols = int(getattr(data, "num_cols", 0) or 0)
        cells = [["" for _ in range(num_cols)] for _ in range(num_rows)]
        merged: list[tuple[int, int, int, int]] = []
        for cell in getattr(data, "table_cells", ()) or ():
            row = int(getattr(cell, "start_row_offset_idx", 0) or 0)
            col = int(getattr(cell, "start_col_offset_idx", 0) or 0)
            row_span = int(getattr(cell, "row_span", 1) or 1)
            col_span = int(getattr(cell, "col_span", 1) or 1)
            text = str(getattr(cell, "text", "") or "").strip()
            if not (0 <= row < num_rows and 0 <= col < num_cols):
                continue
            for rr in range(row, min(row + row_span, num_rows)):
                for cc in range(col, min(col + col_span, num_cols)):
                    cells[rr][cc] = text
            if row_span > 1 or col_span > 1:
                merged.append((row + 1, row + row_span, col + 1, col + col_span))
    except Exception:
        return None
    return _grid_from_rows(
        cells,
        title=title,
        merged=tuple(merged),
        source="docling",
        confidence=0.75,
    )


class PaddleStructureAdapter:
    """PaddleOCR PP-Structure（扫描/中文表格）；未安装时不可用。"""

    name = "paddle"

    def available(self) -> bool:
        try:
            from paddleocr import PPStructure  # noqa: F401
        except Exception:
            return False
        return True

    def extract(self, path: Any, page: int, bbox: Any = None, *, title: str = "") -> TableGrid | None:
        # 具体 OCR/表格管线由部署环境决定；接口先留好，避免把 paddle 变成硬依赖。
        del path, page, bbox, title
        return None


class CloudTableAdapter:
    """云服务占位：只有用户显式设置 HELPER_TABLE_STRUCTURE=cloud 才启用。"""

    name = "cloud"

    def available(self) -> bool:
        return False

    def extract(self, path: Any, page: int, bbox: Any = None, *, title: str = "") -> TableGrid | None:
        del path, page, bbox, title
        return None


_ADAPTERS: dict[str, type[TableStructureAdapter]] = {
    "docling": DoclingAdapter,
    "paddle": PaddleStructureAdapter,
    "cloud": CloudTableAdapter,
}
_INSTANCES: dict[str, TableStructureAdapter | None] = {}


def table_structure_mode() -> str:
    return str(os.environ.get("HELPER_TABLE_STRUCTURE", "off") or "off").strip().lower()


def get_adapter(mode: str | None = None) -> TableStructureAdapter | None:
    """按开关取适配器；off/未知/未安装都返回 None（调用方回退）。"""
    name = (mode or table_structure_mode()).lower()
    if name in ("", "off", "0", "false"):
        return None
    if name in _INSTANCES:
        return _INSTANCES[name]
    cls = _ADAPTERS.get(name)
    adapter = None
    if cls is not None:
        candidate = cls()
        adapter = candidate if candidate.available() else None
    _INSTANCES[name] = adapter
    return adapter
