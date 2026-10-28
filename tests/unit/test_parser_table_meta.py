"""DOCX/PPTX 表格标题与合并元数据。"""

from __future__ import annotations

from app.core.parse import parse_file
from app.core.parse.model import TYPE_TABLE


def _table_block(parsed):
    return next(block for block in parsed.blocks if block.content_type == TYPE_TABLE)


def test_docx_caption_and_vertical_merge(tmp_path):
    from docx import Document as Docx

    doc = Docx()
    caption = doc.add_paragraph("表 1 销售明细")
    caption.style = "Caption"
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "区域"
    table.cell(0, 1).text = "金额"
    table.cell(1, 0).text = "华东"
    table.cell(1, 1).text = "1200"
    # 纵向合并首列：第二行应继承“华东”，而不是丢值
    table.cell(1, 0).merge(table.cell(0, 0))
    path = tmp_path / "a.docx"
    doc.save(path)

    block = _table_block(parse_file(path))
    assert block.table_title == "表 1 销售明细"
    assert block.merged_ranges
    assert "华东" in block.text


def test_pptx_title_and_merged_table(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "2026 销售表"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(4), Inches(1)).table
    table.cell(0, 0).text = "区域"
    table.cell(0, 1).text = "金额"
    table.cell(1, 0).text = "华东"
    table.cell(1, 1).text = "1200"
    table.cell(0, 0).merge(table.cell(1, 0))
    path = tmp_path / "a.pptx"
    prs.save(path)

    block = _table_block(parse_file(path))
    assert block.table_title == "2026 销售表"
    assert block.merged_ranges
    # 合并区的值铺到覆盖行
    assert block.text.count("区域") >= 2
