"""xlsx 结构化表格：空列保留、合并单元格展开、表头/行列号、引用定位。"""

from __future__ import annotations

import os
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment

from app.core.parse.model import TYPE_TABLE


def _write_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "表A"
    ws.append(["年度", "公司", "金额"])
    ws.append(["", "华为", 100])
    ws["A2"] = 2019
    ws.merge_cells("A2:A3")  # 2019 纵向合并覆盖两行
    ws.append(["", "阿里", 200])  # A 被合并填充为 2019
    ws.append([])  # 空行 = 段边界
    ws.append([2021, "腾讯", 300])
    ws.append([2022, "字节", ""])  # 末列空，但要保留位置
    wb.save(path)


def test_xlsx_parser_structured_table(tmp_path: Path) -> None:
    from app.core.parse import iter_units

    src = tmp_path / "t.xlsx"
    _write_xlsx(src)

    tables: list[object] = []
    for unit in iter_units(src):
        for block in unit.blocks:
            if block.content_type == TYPE_TABLE:
                tables.append(block)

    assert len(tables) == 2
    first = tables[0]
    # 合并单元格展开后，第二行 A 列也被填成 2019；空列不丢
    assert "年度: 2019 | 公司: 华为 | 金额: 100" in first.text
    assert "年度: 2019 | 公司: 阿里 | 金额: 200" in first.text
    # 第二段：末列空值省略，但行仍带列名
    second = tables[1]
    assert "年度: 2022 | 公司: 字节" in second.text
    assert tuple(first.header) == ("年度", "公司", "金额")
    assert first.row_from == 2 and first.row_to == 3
    assert first.col_from == 1 and first.col_to == 3
    # 不再把裸表头行塞进 child；首行就是数据行
    assert first.text.splitlines()[0] == "年度: 2019 | 公司: 华为 | 金额: 100"


def test_xlsx_multilevel_header_is_flattened(tmp_path: Path) -> None:
    """两级表头：一级合并组 + 二级叶子列，拼成 ``财务/年度`` 并去重。"""
    from app.core.parse import iter_units

    wb = Workbook()
    ws = wb.active
    ws.title = "表B"
    ws["A1"] = "财务"
    ws.merge_cells("A1:B1")  # 一级：财务 覆盖 A、B
    ws["C1"] = "说明"
    ws["A2"] = "年度"
    ws["B2"] = "期间"
    ws.append([2019, 7, "x"])
    src = tmp_path / "multi.xlsx"
    wb.save(src)

    tables = [
        block
        for unit in iter_units(src)
        for block in unit.blocks
        if block.content_type == TYPE_TABLE
    ]
    assert len(tables) == 1
    table = tables[0]
    assert tuple(table.header) == ("财务/年度", "财务/期间", "说明")
    assert "财务/年度: 2019 | 财务/期间: 7 | 说明: x" in table.text
    # 文本里没有裸表头行；第一条数据映射回真实 row=3
    assert table.row_from == 3
    assert table.text.splitlines()[0] == "财务/年度: 2019 | 财务/期间: 7 | 说明: x"


def test_xlsx_cell_comment_is_appended(tmp_path: Path) -> None:
    """单元格批注（如 F4 的测试注释）要拼到值后面，不能丢。"""
    from app.core.parse import iter_units

    wb = Workbook()
    ws = wb.active
    ws.title = "表C"
    ws.append(["名称", "金额"])
    ws.append(["HW企业IT事业部", 100])
    ws["A2"].comment = Comment("作者:\n这个是一个测试注释", "tester")
    src = tmp_path / "comment.xlsx"
    wb.save(src)

    tables = [
        block
        for unit in iter_units(src)
        for block in unit.blocks
        if block.content_type == TYPE_TABLE
    ]
    assert tables
    assert "名称: HW企业IT事业部（这个是一个测试注释）" in tables[0].text


def test_xlsx_table_chunks_carry_locator(tmp_path: Path) -> None:
    from app.core.chunk import Splitter
    from app.core.parse import iter_units
    from app.core.parse.structure import Structurer

    src = tmp_path / "t.xlsx"
    _write_xlsx(src)

    splitter = Splitter.from_config(semantic=lambda _text, _budget: None)
    structure = Structurer(toc=None, doc_title_from_h1=False)
    records: list[object] = []
    for unit in iter_units(src):
        blocks = structure.feed(unit.blocks)
        splitter.set_doc_title(structure.doc_title)
        for group in splitter.feed(blocks):
            records.extend(group.children)
    for group in splitter.flush():
        records.extend(group.children)

    table_records = [r for r in records if r.content_type == TYPE_TABLE]
    assert table_records
    summaries = [r for r in table_records if "表摘要：" in r.text]
    assert summaries and "列：" in summaries[0].text
    assert summaries[0].text.startswith("表A\n")
    first = next(r for r in table_records if "表摘要：" not in r.text)
    assert tuple(first.header) == ("年度", "公司", "金额")
    assert first.row_from == 2 and first.row_to == 2
    assert first.col_from == 1 and first.col_to == 3
    loc = first.loc_json()
    assert loc["kind"] == "table"
    assert loc["table"] == "sheet:表A#1"
    assert loc["row_from"] == 2 and loc["row_to"] == 2
    assert loc["header"] == ["年度", "公司", "金额"]
    # 标题在 heading/section_path，正文只放行数据（title/text 分开存）
    assert first.section_path[-1] == "表A"
    assert first.text.startswith("年度: 2019")


def test_exstruct_optional_backend_falls_back_when_unavailable(tmp_path: Path) -> None:
    """未装 exstruct（或没开开关）时：可选后端不启用，openpyxl 路径照常工作。"""
    from app.core.parse import iter_units, xlsx_exstruct

    old = os.environ.get("HELPER_XLSX_BACKEND")
    os.environ["HELPER_XLSX_BACKEND"] = "exstruct"
    try:
        # 无论 exstruct 是否安装可用，xlsx 都必须能解析（装了走它 / 否则回退 openpyxl）
        assert xlsx_exstruct.available() in (True, False)
        src = tmp_path / "t.xlsx"
        _write_xlsx(src)
        tables = [
            block
            for unit in iter_units(src)
            for block in unit.blocks
            if block.content_type == TYPE_TABLE
        ]
        assert tables, "xlsx 解析在可选后端开关打开时也要产出表格块"
    finally:
        if old is None:
            os.environ.pop("HELPER_XLSX_BACKEND", None)
        else:
            os.environ["HELPER_XLSX_BACKEND"] = old


def test_cell_newlines_stay_in_one_row() -> None:
    """单元格内换行折叠成 /，避免一个 Excel 行被切成多块。"""
    from app.core.parse.xlsx import _cell_text

    text = _cell_text("A. 选项一\nB. 选项二\nC. 选项三")
    assert "\n" not in text  # 不再带换行
    assert text == "A. 选项一 / B. 选项二 / C. 选项三"  # 分隔语义保留
