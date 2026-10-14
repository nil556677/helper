"""资料导入：解析、切分、判重、重建、删除。

这些用例钉住四件事：

1. **常用格式真的能读**（txt/md/csv/json/html + docx/xlsx/pptx 往返，PDF 坏文件要给人话）；
2. **判重分两级**（字节相同 → 零解析跳过；换了名字/排版的同一篇 → 也跳过）；
3. **失败也进列表**（带原因）——只弹一次通知的话，用户关掉就再也找不到"哪一份失败了"；
4. **索引文本与正文一起写**（``chunks.tokens`` 必须是分好词的，否则"看得见、搜不到"）。
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest

SAMPLE = "# 设备采购合同\n\n第一条 乙方应在 30 日内交付设备。\n\n第二条 违约金按日万分之五计算。\n"


@pytest.fixture
def ingest_db(fake_root: Path) -> Iterator[None]:
    """一个建好表、目录隔离在 ``.tmp/`` 下的空库。"""
    from app.core import paths
    from app.core.db import schema
    from app.core.db.connection import close_thread_connection

    paths.ensure_dirs()
    schema.init_db()
    yield
    close_thread_connection()


# ---------------- 解析 ----------------


def test_text_is_decoded_even_when_saved_as_gbk() -> None:
    """国内机器上"另存为 ANSI"很常见：GBK 也要读得出来，而不是抛解码错。"""
    from app.core.features.rag.ingest import parsers

    parsed = parsers.parse("设备验收：合格。".encode("gbk"), "验收.txt")
    assert "合格" in parsed.text


def test_csv_becomes_one_line_per_row() -> None:
    """表格按行展开比原样逗号文本好命中。"""
    from app.core.features.rag.ingest import parsers

    parsed = parsers.parse("型号,数量\nA-1,3\nB-2,5\n".encode(), "清单.csv")
    assert parsed.text.splitlines() == ["型号 | 数量", "A-1 | 3", "B-2 | 5"]


def test_json_is_flattened_to_key_value_lines() -> None:
    """JSON 拍平成"键: 值"的行，键名也能命中。"""
    from app.core.features.rag.ingest import parsers

    parsed = parsers.parse(b'{"contract": {"penalty": "0.05%"}}', "meta.json")
    assert "contract.penalty: 0.05%" in parsed.text


def test_html_drops_tags_and_scripts() -> None:
    """HTML 只留能读的文字：script/style 里的东西不是内容。"""
    from app.core.features.rag.ingest import parsers

    html = (
        b"<html><head><style>p{color:red}</style></head>"
        b"<body><p>hello</p><script>x=1</script></body></html>"
    )
    parsed = parsers.parse(html, "page.html")
    assert "hello" in parsed.text
    assert "color:red" not in parsed.text
    assert "x=1" not in parsed.text


def test_unsupported_suffix_says_what_to_do() -> None:
    """不支持就说不支持，并且告诉他怎么办（"另存为 .xlsx"）。"""
    from app.core.features.rag.ingest import parsers

    with pytest.raises(parsers.ParseError) as caught:
        parsers.parse(b"\xd0\xcf\x11\xe0", "老表格.xls")
    assert "另存为 .xlsx" in str(caught.value)


def test_broken_pdf_fails_with_a_readable_reason() -> None:
    """坏 PDF 不能把解析库的异常原样抛给用户。"""
    from app.core.features.rag.ingest import parsers

    with pytest.raises(parsers.ParseError) as caught:
        parsers.parse(b"this is definitely not a pdf", "合同.pdf")
    assert "PDF" in str(caught.value)


def test_docx_round_trip() -> None:
    """自己造一份 Word → 解析器要能把段落与表格都抠出来。"""
    docx = pytest.importorskip("docx")
    from app.core.features.rag.ingest import parsers

    document = docx.Document()
    document.add_paragraph("第一条 乙方应在 30 日内交付。")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "型号"
    table.cell(0, 1).text = "A-1"
    buffer = io.BytesIO()
    document.save(buffer)

    parsed = parsers.parse(buffer.getvalue(), "合同.docx")
    assert "30 日内交付" in parsed.text
    assert "型号 | A-1" in parsed.text


def test_xlsx_round_trip() -> None:
    """Excel：每个工作表逐行展开，表名也要留着（不然不知道这几行是哪张表）。"""
    openpyxl = pytest.importorskip("openpyxl")
    from app.core.features.rag.ingest import parsers

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "设备清单"
    sheet.append(["型号", "数量"])
    sheet.append(["A-1", 3])
    buffer = io.BytesIO()
    workbook.save(buffer)

    parsed = parsers.parse(buffer.getvalue(), "清单.xlsx")
    assert "# 设备清单" in parsed.text
    assert "A-1 | 3" in parsed.text


def test_pptx_round_trip() -> None:
    """PPT：按页抠文本框，并记住页数。"""
    pptx = pytest.importorskip("pptx")
    from app.core.features.rag.ingest import parsers

    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "验收结论"
    buffer = io.BytesIO()
    presentation.save(buffer)

    parsed = parsers.parse(buffer.getvalue(), "汇报.pptx")
    assert "验收结论" in parsed.text
    assert parsed.pages == 1


def test_supported_suffixes_cover_the_common_formats() -> None:
    """界面上"支持哪些格式"是从这张表生成的——它必须真的覆盖常用格式。"""
    from app.core.features.rag.ingest import parsers

    supported = set(parsers.supported_suffixes())
    assert {".txt", ".md", ".csv", ".json", ".html", ".pdf", ".docx", ".xlsx", ".pptx"} <= supported


# ---------------- 切分 ----------------


def test_split_keeps_paragraphs_apart() -> None:
    """跨段落不合并：空行是作者给的语义边界，混在一起会让分数被稀释。"""
    from app.core.features.rag.ingest.splitter import split_text

    chunks = split_text("第一段讲付款。\n\n第二段讲验收。", max_chars=300)
    assert chunks == ["第一段讲付款。", "第二段讲验收。"]


def test_split_hard_splits_a_giant_sentence() -> None:
    """没有标点的超长文本要硬切，不能切成一块几千字的。"""
    from app.core.features.rag.ingest.splitter import split_text

    chunks = split_text("甲" * 250, max_chars=100)
    assert [len(chunk) for chunk in chunks] == [100, 100, 50]


# ---------------- 入库 / 判重 / 重建 / 删除 ----------------


def _import(name: str, data: bytes):
    """直接走 core 的导入入口。

    服务层那条同步的 `import_uploads` 已经取消：导入现在由**任务队列**调用 `pipeline.import_bytes`
    （见 tests/unit/test_import_job.py）。这里测的是导入本身：解析、切分、判重、副本。
    """
    from app.core.features.rag.ingest import pipeline

    return pipeline.import_bytes(data, name)


def test_import_writes_chunks_with_tokens(ingest_db: None) -> None:
    """`chunks.tokens` 必须是**分好词的**：FTS5 拿它当索引，原文进 text。"""
    from app.core.db.connection import get_connection

    result = _import("合同.md", SAMPLE.encode())
    assert result.status == "imported"
    assert result.chunks >= 2

    rows = get_connection().execute("SELECT text, tokens FROM chunks ORDER BY seq").fetchall()
    assert rows, "片段应该写进去了"
    assert any("违约金" in row["tokens"].split() for row in rows), "分词结果里应该有「违约金」这个词"


def test_same_bytes_is_skipped_without_parsing(ingest_db: None) -> None:
    """第一级：字节完全一样 → 直接跳过。"""
    from app.services import knowledge

    assert _import("合同.md", SAMPLE.encode()).status == "imported"
    again = _import("合同.md", SAMPLE.encode())
    assert again.status == "skipped"
    assert len(knowledge.documents()) == 1


def test_same_text_under_another_name_is_skipped_too(ingest_db: None) -> None:
    """第二级：换了名字、改了排版（字节不同）但正文一样 → 也跳过。"""
    from app.services import knowledge

    assert _import("合同.md", SAMPLE.encode()).status == "imported"
    retyped = SAMPLE.replace("\n\n", "\n \n").replace("设备", "设备 ")  # 字节变了、正文没变
    again = _import("合同副本.md", retyped.encode())
    assert again.status == "skipped", again.reason
    assert len(knowledge.documents()) == 1


def test_failed_import_is_listed_with_reason(ingest_db: None) -> None:
    """失败也要进列表并写清原因——不然用户只知道"没导进去"，不知道为什么。"""
    from app.services import knowledge

    result = _import("老表格.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    assert result.status == "failed", result.reason

    items = knowledge.documents()
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert "另存为" in items[0]["detail"]
    assert knowledge.stats()["failed"] == 1


def test_copy_is_kept_so_rebuild_works(ingest_db: None) -> None:
    """成功的资料要留副本：重建索引与追溯原文都靠它。"""
    from app.core import paths

    _import("合同.md", SAMPLE.encode())
    copies = list(paths.files_dir().glob("*/*"))
    assert copies, "应该留了一份副本"
    assert copies[0].read_bytes() == SAMPLE.encode()


def test_rebuild_regenerates_chunks(ingest_db: None) -> None:
    """重建：用副本重新解析切分，片段数对得上，而且不会翻倍。"""
    from app.services import knowledge
    from app.core.db.connection import get_connection

    _import("合同.md", SAMPLE.encode())
    doc_id = knowledge.documents()[0]["id"]
    before = get_connection().execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]

    assert knowledge.rebuild(doc_id)["ok"] is True
    after = get_connection().execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]
    assert after == before


def test_delete_removes_document_chunks_and_copy(ingest_db: None) -> None:
    """删除要连带片段、索引与副本一起清掉（外键级联 + 触发器负责索引）。"""
    from app.services import knowledge
    from app.core import paths
    from app.core.db.connection import get_connection

    _import("合同.md", SAMPLE.encode())
    doc_id = knowledge.documents()[0]["id"]
    assert knowledge.delete([doc_id]) == 1

    assert knowledge.documents() == []
    assert get_connection().execute("SELECT count(*) AS n FROM chunks").fetchone()["n"] == 0
    assert get_connection().execute("SELECT count(*) AS n FROM chunks_fts").fetchone()["n"] == 0
    assert not list(paths.files_dir().glob("*/*")), "副本也该清掉"
