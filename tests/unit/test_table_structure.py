"""表格结构适配器：网格转换、默认关闭、不可用回退。"""

from __future__ import annotations

from app.integrations.table_structure import TableGrid, get_adapter, table_structure_mode


def test_table_grid_to_block_parts():
    grid = TableGrid(
        cells=(("区域", "金额"), ("华东", "1200")),
        merged_ranges=((1, 2, 1, 1),),
        title="表 1",
    )
    text, header, merged = grid.to_block_parts()
    assert text == "区域 | 金额\n华东 | 1200"
    assert header == ("区域", "金额")
    assert merged == ((1, 2, 1, 1),)


def test_default_mode_is_off(monkeypatch):
    monkeypatch.delenv("HELPER_TABLE_STRUCTURE", raising=False)
    assert table_structure_mode() == "off"
    assert get_adapter() is None


def test_unknown_or_unavailable_adapter_returns_none(monkeypatch):
    monkeypatch.setenv("HELPER_TABLE_STRUCTURE", "no_such_adapter")
    assert get_adapter() is None
    # docling 未安装时必须安全返回 None，而不是抛 ImportError
    monkeypatch.setenv("HELPER_TABLE_STRUCTURE", "docling")
    adapter = get_adapter()
    assert adapter is None or adapter.name == "docling"


def test_pdf_structure_hook_uses_adapter(monkeypatch):
    from pathlib import Path

    from app.core.parse import pdf_meta

    grid = TableGrid(cells=(("区域", "金额"), ("华东", "1200")), title="表 1")

    class Fake:
        def extract(self, path, page, bbox=None, *, title=""):
            return grid

    monkeypatch.setattr(
        "app.integrations.table_structure.get_adapter", lambda: Fake()
    )
    assert pdf_meta.structure_grid(Path("x.pdf"), 1, (0, 0, 10, 10)) is grid
