"""各格式解析产出的结构元数据：xlsx 的 sheet 标题、PPT 式文档标题开关。"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_json_is_not_supported(tmp_path: Path) -> None:
    """JSON 字段语义不明确，不再作为可解析格式；上传/解析都要明确拒绝。"""
    from app.core.parse import ParseError, parse_file, supported_suffixes

    path = tmp_path / "a.json"
    path.write_text('[{"code": "1101", "name": "市辖区"}]', encoding="utf-8")
    assert ".json" not in supported_suffixes()
    with pytest.raises(ParseError, match="JSON"):
        parse_file(path, name="a.json")


def test_log_is_parsed_per_line_and_not_collapsed(tmp_path: Path) -> None:
    """日志按行成块，切分后不能只剩一个子块（旧实现会把前面几百字从子块里丢掉）。"""
    from app.core.chunk import Splitter
    from app.core.parse import parse_file

    lines = [
        f"[2026-06-12 08:{index:02d}:00] INFO  Line02-Device005 第 {index} 条日志，模块运行正常"
        for index in range(12)
    ]
    path = tmp_path / "a.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    parsed = parse_file(path, name="a.log")
    assert len(parsed.blocks) == 12, "日志应一行一个块"

    splitter = Splitter.from_config()
    groups = splitter.feed(parsed.blocks) + splitter.flush()
    children = [child for group in groups for child in group.children]
    assert len(children) >= 2, "12 行日志不能只剩 1 个子块"
    assert children[0].line_from == 1
    assert children[-1].line_to == 12
    source = "\n".join(lines)
    for child in children:
        assert child.text.strip() in source, "子块文本必须来自原文（不能丢前半截）"


def test_log_like_txt_is_parsed_per_line(tmp_path: Path) -> None:
    """.txt 内容像日志（多数行以时间戳开头）时也应一行一块。"""
    from app.core.parse import parse_file

    lines = [
        f"[2026-06-12 08:{index:02d}:00] INFO  Line02-Device005 第 {index} 条日志，模块运行正常"
        for index in range(12)
    ]
    path = tmp_path / "a.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert len(parse_file(path, name="a.txt").blocks) == 12


def test_plain_txt_keeps_paragraphs(tmp_path: Path) -> None:
    """普通 .txt 仍按段落成块，不能被日志判定误伤。"""
    from app.core.parse import parse_file

    path = tmp_path / "b.txt"
    path.write_text(
        "第一段第一句。\n第一段第二句。\n\n第二段只有一句。\n",
        encoding="utf-8",
    )
    blocks = parse_file(path, name="b.txt").blocks
    assert len(blocks) == 2
    assert blocks[0].text == "第一段第一句。\n第一段第二句。"


def test_markdown_front_matter_lists_and_images(tmp_path: Path) -> None:
    """front matter 变元数据、列表/引用不重复且保留标记、图片出占位块。"""
    from app.core.parse import parse_file
    from app.core.parse.structure import Structurer

    markdown = (
        "---\n"
        "title: 测试文档\n"
        "date: 2026-01-01\n"
        "tags: [a, b]\n"
        "---\n\n"
        "# 一级标题\n\n"
        "- 条目一\n"
        "- 条目二\n"
        "  - 嵌套条目\n\n"
        "1. 有序一\n"
        "2. 有序二\n\n"
        "> 引用一段。\n\n"
        "![图](a.png)\n"
    )
    path = tmp_path / "a.md"
    path.write_text(markdown, encoding="utf-8")
    parsed = parse_file(path, name="a.md")
    texts = [block.text for block in parsed.blocks]

    # front matter 不能变成标题/正文
    assert parsed.blocks[0].content_type == "heading"
    assert parsed.blocks[0].text == "测试文档"
    assert not any("title:" in text or "date:" in text for text in texts)
    structurer = Structurer()
    structurer.feed(parsed.blocks)
    assert structurer.doc_title == "测试文档"

    # 列表/引用只出现一次，且保留标记与嵌套缩进
    assert texts.count("- 条目一") == 1
    assert texts.count("- 条目二") == 1
    assert "  - 嵌套条目" in texts
    assert "1. 有序一" in texts
    assert "2. 有序二" in texts
    assert texts.count("> 引用一段。") == 1

    # 图片是独立占位块，并有提示
    assert any(block.content_type == "image" and block.text == "图" for block in parsed.blocks)
    assert any("图片" in note for note in parsed.notes)


def test_markdown_task_list_marker(tmp_path: Path) -> None:
    """任务列表保留 ``[ ]`` / ``[x]`` 标记（依赖 mdit-py-plugins）。"""
    pytest.importorskip("mdit_py_plugins")
    from app.core.parse import parse_file

    path = tmp_path / "tasks.md"
    path.write_text("# 待办\n\n- [ ] 未完成\n- [x] 已完成\n", encoding="utf-8")
    texts = [block.text for block in parse_file(path, name="tasks.md").blocks]
    assert "- [ ] 未完成" in texts
    assert "- [x] 已完成" in texts


def test_xlsx_sheet_name_becomes_heading(tmp_path: Path) -> None:
    """Excel 没有正文标题，但 sheet 名要作为标题，片段才有章节路径。"""
    import openpyxl

    from app.core.parse import iter_units

    path = tmp_path / "表.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "合同清单"
    sheet.append(["名称", "金额"])
    sheet.append(["设备", 100])
    workbook.save(path)

    blocks = [block for unit in iter_units(path, name="表.xlsx") for block in unit.blocks]
    headings = [block for block in blocks if block.content_type == "heading"]
    assert [block.text for block in headings] == ["合同清单"]
    assert headings[0].level == 1


def test_doc_title_can_be_disabled_for_pptx_like_documents() -> None:
    """PPT 每页标题是页标题，不能把第一页当文档大标题。"""
    from app.core.parse.model import TYPE_HEADING, Block
    from app.core.parse.structure import Structurer

    structurer = Structurer(doc_title_from_h1=False)
    blocks = structurer.feed(
        [
            Block(text="第一页标题", content_type=TYPE_HEADING, level=1),
            Block(text="第一页正文。"),
        ]
    )
    assert structurer.doc_title == ""
    assert list(blocks[1].section_path) == ["第一页标题"]


def test_pptx_group_shapes_and_notes_are_read(tmp_path: Path) -> None:
    """组合形状里的文本、以及演讲者备注都要能读进块。"""
    import contextlib

    from pptx import Presentation
    from pptx.util import Inches

    path = tmp_path / "group.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])  # 空白版式
    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text_frame.text = "组合里的正文"
    with contextlib.suppress(Exception):
        slide.notes_slide.notes_text_frame.text = "给演讲者的提示语"
    presentation.save(path)

    from app.core.parse import iter_units

    blocks = [block for unit in iter_units(path, name="group.pptx") for block in unit.blocks]
    texts = [block.text for block in blocks]
    assert any("组合里的正文" in t for t in texts), "组合形状文本应被读到"
    assert any("演讲者备注" in t and "提示语" in t for t in texts), "演讲者备注应被读到"


def test_html_document_title_from_h1(tmp_path: Path) -> None:
    """HTML 首个 ``<h1>`` 要成为文档标题，而不是退回文件名（``doc.htm`` → "doc"）。"""
    from app.core.parse import parse_file
    from app.core.parse.structure import Structurer

    path = tmp_path / "doc.htm"
    path.write_text(
        "<html><head><title>关于测试的文章标题__某某站</title></head><body>"
        "<div class='nav'>首页</div>"
        "<h1>关于测试的文章标题</h1>"
        "<p>这是正文第一段，用来确认标题不会被当成普通文本。</p>"
        "</body></html>",
        encoding="utf-8",
    )
    parsed = parse_file(path)
    # 首块必须是原生 H1（导入层按首块是不是 H1 决定要不要用文件名兜底）
    assert parsed.blocks[0].content_type == "heading"
    assert parsed.blocks[0].level == 1
    assert parsed.blocks[0].text == "关于测试的文章标题"
    # 模拟导入链路：结构识别应把首块记成文档大标题
    structurer = Structurer()
    structurer.feed(parsed.blocks)
    assert structurer.doc_title == "关于测试的文章标题"


def test_html_title_falls_back_to_head_title(tmp_path: Path) -> None:
    """正文没有 ``<h1>`` 时用 ``<title>`` 兜底，并去掉站点名尾巴。"""
    from app.core.parse import parse_file

    path = tmp_path / "doc.html"
    path.write_text(
        "<html><head><title>只有标题标签的文章__示例站</title></head>"
        "<body><p>正文。</p></body></html>",
        encoding="utf-8",
    )
    parsed = parse_file(path)
    assert parsed.blocks[0].content_type == "heading"
    assert parsed.blocks[0].text == "只有标题标签的文章"


def test_html_nested_header_is_fully_skipped(tmp_path: Path) -> None:
    """页头里嵌套的 ``<div>`` 不能提前关掉跳过区（否则漏出 EN / 站点 URL 噪声）。"""
    from app.core.parse import parse_file

    path = tmp_path / "page.htm"
    path.write_text(
        "<html><body>"
        "<div class='header'><div class='header_logo'>LOGO</div>"
        "<a class='lang'>EN</a></div>"
        "<div class='siteurl hide'>https://example.com/</div>"
        "<h1>正文标题</h1><p>正文第一段，长度要够。</p>"
        "</body></html>",
        encoding="utf-8",
    )
    texts = [block.text for block in parse_file(path).blocks]
    assert "LOGO" not in texts
    assert "EN" not in texts
    assert not any("example.com" in text for text in texts)
    assert any("正文第一段" in text for text in texts)
    assert texts[0] == "正文标题"


def test_html_article_meta_is_not_body(tmp_path: Path) -> None:
    """日期/来源等文章元信息不该进正文；正文里以年份开头的句子不能被误删。"""
    from app.core.parse import parse_file

    path = tmp_path / "article.htm"
    path.write_text(
        "<html><head><title>文章标题</title></head><body>"
        "<h1>文章标题</h1>"
        "<div class='pages-date'>2026-10-18 08:40\n<span>来源：人民日报</span></div>"
        "<p>这是正文第一段，长度要够，用来确认元信息不会混进来。</p>"
        "<p>2026年，正文里以年份开头的句子不能被误删，所以这里写长一点。</p>"
        "</body></html>",
        encoding="utf-8",
    )
    texts = [block.text for block in parse_file(path).blocks]
    assert texts[0] == "文章标题"
    assert not any("来源" in text for text in texts)
    assert not any("2026-10-18" in text for text in texts)
    assert any("以年份开头" in text for text in texts), "正文里的日期开头句子不应被误删"


def test_txt_article_meta_is_not_body(tmp_path: Path) -> None:
    """TXT 开头的日期/来源行不该进正文。"""
    from app.core.parse import iter_units

    path = tmp_path / "a.txt"
    path.write_text(
        "2024-03-15 来源：人民日报\n\n这是正文第一段。\n\n第二段正文。\n",
        encoding="utf-8",
    )
    texts = [block.text for unit in iter_units(path, name="a.txt") for block in unit.blocks]
    assert not any("来源" in text for text in texts)
    assert any("正文第一段" in text for text in texts)


def test_md_article_meta_is_not_body(tmp_path: Path) -> None:
    """Markdown 的日期/来源段落不该进正文，标题照常保留。"""
    from app.core.parse import iter_units

    path = tmp_path / "a.md"
    path.write_text(
        "# 标题\n\n2024-03-15 来源：人民日报\n\n这是正文第一段。\n",
        encoding="utf-8",
    )
    texts = [block.text for unit in iter_units(path, name="a.md") for block in unit.blocks]
    assert "标题" in texts
    assert not any("来源" in text for text in texts)
    assert any("正文第一段" in text for text in texts)


def test_docx_article_meta_is_not_body(tmp_path: Path) -> None:
    """DOCX 的日期/来源段落不该进正文。"""
    import docx

    from app.core.parse import iter_units

    path = tmp_path / "a.docx"
    document = docx.Document()
    document.add_paragraph("2024-03-15 来源：人民日报")
    document.add_paragraph("这是正文第一段。")
    document.save(path)

    texts = [block.text for unit in iter_units(path, name="a.docx") for block in unit.blocks]
    assert not any("来源" in text for text in texts)
    assert any("正文第一段" in text for text in texts)


def test_html_doc_title_is_stored(rag_db: None) -> None:
    """端到端：导入 HTML 后 ``documents.doc_title`` 是文章标题，不是文件名 ``doc``。"""
    import numpy as np

    from app.core.config import store as params_store
    from app.rag.ingest import importer
    from app.rag.store import documents

    def fake_embed(texts):
        size = int(params_store.load()["embed.dims"])
        rows = []
        for text in texts:
            vector = np.zeros(size, dtype=np.float32)
            vector[abs(hash(text)) % size] = 1.0
            rows.append(vector)
        return np.stack(rows)

    html = (
        "<html><head><title>测试文章标题__示例站</title></head><body>"
        "<h1>测试文章标题</h1>"
        "<p>这是正文第一段，内容要足够长才能切出可检索的片段，所以这里多写几句完整的话。</p>"
        "<p>这是正文第二段，同样写成长句，用来模拟真实文章而不是只有标题的占位内容。</p>"
        "</body></html>"
    )
    result = importer.import_bytes(html.encode("utf-8"), "doc.htm", embed=fake_embed)
    assert result.status == "imported", result.reason
    assert result.doc_id is not None
    row = documents.get(int(result.doc_id))
    assert row is not None
    assert row["doc_title"] == "测试文章标题"
