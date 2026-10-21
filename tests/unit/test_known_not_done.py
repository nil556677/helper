"""本轮"已知未做"开发项的定向单测：硬切回退、行号口径、检索数据源。"""

from __future__ import annotations


def test_hard_cut_backs_off_to_punctuation() -> None:
    """超预算硬切时，宁可回退 ~40 字也要切在标点/空白后，不劈词/句。"""
    from app.core.chunk.core import _last_break_before_end

    segment = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉。后面还有一大段无论如何也要继续写的内容"
    cut = _last_break_before_end(segment)
    assert cut > 0
    # 切点必须在句号"。"的后面
    assert segment[cut - 1] in "。！？；，"
    assert cut <= len(segment)

    # 整段都是连续的字符、没有标点 → 返回 0（不回退，保持原预算长度）
    assert _last_break_before_end("甲" * 120) == 0
    # 窗口内只有空白也要回到空白后
    spaced = "这是很长的一段文字" + " " + "接着还有"
    assert _last_break_before_end(spaced) > 0


def test_table_loc_carries_row_base() -> None:
    """表格定位要标注行号口径：sheet 绝对 vs 表内。"""
    from app.rag.store.chunks import _loc_with_row_base

    sheet = _loc_with_row_base({"kind": "table", "row_from": 7, "row_to": 9}, "sheet:利润中心#1")
    assert sheet["row_base"] == "sheet-absolute"
    table = _loc_with_row_base({"kind": "table", "row_from": 1, "row_to": 3}, "tbl#1")
    assert table["row_base"] == "table"
    # 非表格定位不标（保持原样）
    page = _loc_with_row_base({"kind": "page", "page": 5}, None)
    assert "row_base" not in page


def test_retrieval_origin_and_answer() -> None:
    """把检索 payload 转成人类可读出处、并把父块拼成抽取式答案。"""
    from app.ui.screens import _chat_retrieval
    from app.ui.state.chat_model import Fragment

    assert _chat_retrieval._origin({"loc": {"kind": "page", "page": 3}}) == "第 3 页"
    assert _chat_retrieval._origin({"loc": {"kind": "para", "para": 2}}) == "第 3 段"
    assert _chat_retrieval._origin({"loc": {"kind": "table", "row_from": 2, "row_to": 4}}) == "表格 · 第 2–4 行"
    assert _chat_retrieval._origin({"heading": "研究 › 结论"}) == "研究 › 结论"

    answer = _chat_retrieval._answer(
        [Fragment(index=1, source_name="合同.md", origin="第 5 段", score=4.8, text="违约金千分之三")]
    )
    assert "合同.md" in answer and "千分之三" in answer
    assert _chat_retrieval._answer([]) == ""
