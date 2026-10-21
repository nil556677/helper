"""切分行为快照（golden）：改算法时这些断言能立刻暴露边界变化。

覆盖：整句不切、无标点硬切+诊断、软边界优先、图片跳过、表格单行超预算硬切。
"""

from __future__ import annotations

from app.core.chunk import Splitter
from app.core.parse.model import TYPE_IMAGE, TYPE_TABLE, Block

#: 与生产一致的小尺寸（300 token ≈ 435 汉字）
CHILD = 300


def _split(blocks: list[Block]) -> tuple[Splitter, list]:
    splitter = Splitter(
        parent_size=CHILD * 5,
        parent_overlap=CHILD // 2,
        child_size=CHILD,
        child_overlap=CHILD * 15 // 100,
    )
    groups = splitter.feed(blocks) + splitter.flush()
    return splitter, groups


def _children(groups: list) -> list:
    return [child for group in groups for child in group.children]


def test_complete_sentence_is_not_split() -> None:
    """能整句放下就**不切**（哪怕多句拼一起也保持完整）。"""
    splitter, groups = _split([Block(text="第一句很短。第二句也很短。")])
    children = _children(groups)
    assert len(children) == 1
    assert children[0].text == "第一句很短。第二句也很短。"
    assert splitter.sink.items() == []


def test_no_punctuation_long_text_is_hard_cut_and_reported() -> None:
    """没有可用标点的超长段：硬切 + **必须有诊断**（不静默）。"""
    splitter, groups = _split([Block(text="啊" * 2000)])
    children = _children(groups)
    assert len(children) > 1
    assert all(len(child.text) <= 500 for child in children)
    codes = {item.code for item in splitter.sink.items()}
    assert "hard_cut_used" in codes


def test_comma_soft_boundary_avoids_hard_cut() -> None:
    """有软边界（逗号）时优先在边界切，不触发硬切诊断。"""
    text = "甲" * 250 + "，" + "乙" * 250  # 整段超预算，但每侧都放得下
    splitter, groups = _split([Block(text=text)])
    children = _children(groups)
    assert len(children) == 2
    assert children[0].text.startswith("甲")
    assert children[1].text.startswith("乙")
    codes = {item.code for item in splitter.sink.items()}
    assert "hard_cut_used" not in codes


def test_image_block_is_skipped_with_info() -> None:
    """图片块不产片段，但记一条 info（用户能看到"跳过了图片"）。"""
    splitter, groups = _split([Block(text="", content_type=TYPE_IMAGE)])
    assert _children(groups) == []
    codes = {item.code for item in splitter.sink.items()}
    assert "image_skipped" in codes


def test_table_single_row_over_budget_is_hard_cut() -> None:
    """表格单行超预算：按字符硬切，并记告警（以前会产出超预算块）。"""
    splitter, groups = _split([Block(text="表头\n" + "列" * 2000, content_type=TYPE_TABLE)])
    children = _children(groups)
    assert children, "超长表格行也要切出片段"
    assert max(len(child.text) for child in children) <= 500
    codes = {item.code for item in splitter.sink.items()}
    assert "atomic_line_over_budget" in codes


def test_heading_before_table_is_consumed() -> None:
    """标题后紧跟表格：标题进 section_path 元数据，父块正文保持纯内容。"""
    from app.core.parse.model import TYPE_HEADING, TYPE_TABLE

    splitter, groups = _split(
        [
            Block(text="4.3 实验结果", content_type=TYPE_HEADING, level=2),
            Block(text="表头\n1 | 2", content_type=TYPE_TABLE),
            Block(text="表格后的正文。"),
        ]
    )
    del splitter
    assert groups, "至少要切出组"
    # 标题只作为元数据（section_path），不写进父块正文
    assert list(groups[0].parent.section_path) == ["4.3 实验结果"]
    assert "4.3 实验结果" not in groups[0].parent.text
    # 标题不串到后面的正文组
    assert all("4.3 实验结果" not in group.parent.text for group in groups[1:])


def test_short_front_matter_words_are_not_byline() -> None:
    """文档开头的"目录/前言"这类短行不能被当成作者行。"""
    from app.core.parse.structure import looks_like_byline

    for word in ("目录", "前言", "参考文献"):
        assert looks_like_byline(word) is False
    assert looks_like_byline("赵六") is True
    assert looks_like_byline("(XX 大学计算机科学与技术学院,北京 100084)") is True


def test_numbered_first_heading_is_not_doc_title() -> None:
    """带编号的第一个 H1（"第1章 绪论"）是章节，不是文档大标题。"""
    from app.core.parse.model import TYPE_HEADING
    from app.core.parse.structure import Structurer

    structurer = Structurer()
    blocks = structurer.feed(
        [
            Block(text="第1章 绪论", content_type=TYPE_HEADING, level=1),
            Block(text="本章介绍研究背景。"),
        ]
    )
    assert structurer.doc_title == ""
    assert list(blocks[1].section_path) == ["第1章 绪论"]


def test_official_document_lines_are_not_byline() -> None:
    """公文标题/主送机关不能被当成作者行。"""
    from app.core.parse.structure import looks_like_byline

    for text in ("关于表彰XX学院先进集体的通知", "各学院、各部门：", "通知"):
        assert looks_like_byline(text) is False


def test_section_kind_marks_front_and_back_matter() -> None:
    """头尾内容只标位置，不伪造章节。"""
    from app.services.knowledge.queries import _section_kind

    assert _section_kind({"seq": 0, "section_path": "[]"}, 2) == "front_matter"
    assert _section_kind({"seq": 5, "section_path": "[]"}, 2) == "back_matter"
    assert _section_kind({"seq": 3, "section_path": '["1. 引言"]'}, 2) == "section"


def test_byline_is_its_own_block_and_not_merged_into_body() -> None:
    """作者/单位行：识别成 byline，独立成块，不与摘要粘在一起。"""
    from app.core.parse.model import TYPE_BYLINE, TYPE_HEADING
    from app.core.parse.structure import Structurer

    structurer = Structurer()
    blocks = structurer.feed(
        [
            Block(
                text="大语言模型驱动的中文公文格式自动化研究",
                content_type=TYPE_HEADING,
                level=1,
            ),
            Block(text="赵六"),
            Block(text="(XX 大学计算机科学与技术学院,北京 100084)"),
            Block(text="摘要:这是一段正文。" * 5),
        ]
    )
    assert structurer.doc_title == "大语言模型驱动的中文公文格式自动化研究"
    assert blocks[1].content_type == TYPE_BYLINE
    assert blocks[2].content_type == TYPE_BYLINE
    assert blocks[3].content_type == "text"

    _splitter, groups = _split(blocks)
    bylines = [child for group in groups for child in group.children if child.content_type == TYPE_BYLINE]
    assert bylines, "byline 要有自己的片段"
    assert all("摘要" not in child.text for child in bylines)
