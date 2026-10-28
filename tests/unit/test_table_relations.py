"""表格关系字段 / fetch_mode 取数回归。"""

from __future__ import annotations

from langchain_core.documents import Document

from app.core.chunk.core import Splitter
from app.core.db import schema_v1
from app.core.db.connection import connect, get_connection
from app.core.parse.model import TYPE_TABLE, Block
from app.rag import kb_store
from app.rag.evidence import build_evidence, compose_candidates


def _init(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


def test_table_rows_get_fetch_mode(fake_root):
    _init(fake_root)
    splitter = Splitter(
        parent_size=400,
        parent_overlap=20,
        child_size=30,
        child_overlap=5,
        min_child_size=1,
    )
    block = Block(
        text="区域: 华东 | 产品: A | 金额: 1200",
        content_type=TYPE_TABLE,
        header=("区域", "产品", "金额"),
        row_from=2,
        row_to=2,
    )
    groups = splitter.feed([block]) + splitter.flush()
    assert groups, "表格应产出分组"
    summary = groups[0]
    assert summary.parent.role == "table_parent"
    assert summary.parent.fetch_mode == 0
    rows = [
        child
        for group in groups
        for child in group.children
        if child.role == "table_row"
    ]
    assert rows, "应产出行记录"
    row = rows[0]
    assert row.fetch_mode == 0
    assert row.table_uid and row.row_uid and row.segment_count == 1


def test_wide_row_segments_share_summary_and_order(fake_root):
    _init(fake_root)
    # child_size 足够小，逼出按列组拆分
    splitter = Splitter(
        parent_size=400,
        parent_overlap=20,
        child_size=24,
        child_overlap=4,
        min_child_size=1,
    )
    cells = " | ".join(f"列{i}: 值{i}" for i in range(1, 16))
    block = Block(
        text=cells,
        content_type=TYPE_TABLE,
        header=tuple(f"列{i}" for i in range(1, 16)),
        row_from=2,
        row_to=2,
    )
    groups = splitter.feed([block]) + splitter.flush()
    segments = [
        child
        for group in groups
        for child in group.children
        if child.role == "table_row_segment"
    ]
    assert len(segments) > 1, "超宽行应拆成多个列组"
    assert all(child.fetch_mode == 2 for child in segments)
    assert {child.row_uid for child in segments} == {segments[0].row_uid}
    assert [child.segment_index for child in segments] == list(range(len(segments)))
    assert all(child.segment_count == len(segments) for child in segments)
    parent_uids = {
        group.parent.table_uid
        for group in groups
        if any(child.role == "table_row_segment" for child in group.children)
    }
    assert parent_uids == {segments[0].table_uid}


def test_evidence_mode2_orders_siblings(fake_root):
    _init(fake_root)
    doc_id = kb_store.insert_document(name="a.txt", stored_path="a.txt", file_hash="f" * 64)
    from app.core.db.connection import transaction

    with transaction() as conn:
        parent_id = kb_store.insert_parent(
            conn,
            doc_id=doc_id,
            version=1,
            text="标题\n表摘要",
            role="table_parent",
            fetch_mode=0,
            table_uid="t1",
        )
        for index, text in enumerate(("地区: 华东", "产品: A", "金额: 1200")):
            kb_store.insert_child(
                conn,
                doc_id=doc_id,
                version=1,
                parent_id=parent_id,
                seq=index,
                text=text,
                role="table_row_segment",
                fetch_mode=2,
                table_uid="t1",
                row_uid="r1",
                row_no=2,
                segment_index=index,
                segment_count=3,
                loc={"kind": "table", "table": "t1", "row_from": 2},
            )
    kb_store.activate_version(doc_id, 1)
    meta = {
        "chunk_id": 1,
        "chunk_key": "k",
        "doc_id": doc_id,
        "version": 1,
        "parent_id": parent_id,
        "fetch_mode": 2,
        "role": "table_row_segment",
        "table_uid": "t1",
        "row_uid": "r1",
        "segment_count": 3,
        "source_name": "a.txt",
        "heading": "标题",
        "loc": {"kind": "table", "table": "t1", "row_from": 2},
    }
    evidence = build_evidence([Document(page_content="地区: 华东", metadata=meta)])
    assert evidence and evidence[0]["relation_ok"] is True
    text = evidence[0]["text"]
    assert text.index("地区: 华东") < text.index("产品: A") < text.index("金额: 1200")


def test_compose_candidates_merges_row_before_rerank(fake_root):
    _init(fake_root)
    doc_id = kb_store.insert_document(name="a.txt", stored_path="a.txt", file_hash="e" * 64)
    from app.core.db.connection import transaction

    with transaction() as conn:
        parent_id = kb_store.insert_parent(
            conn,
            doc_id=doc_id,
            version=1,
            text="标题\n表摘要",
            role="table_parent",
            fetch_mode=0,
            table_uid="t2",
        )
        for index, text in enumerate(("地区: 华东", "产品: A", "金额: 1200")):
            kb_store.insert_child(
                conn,
                doc_id=doc_id,
                version=1,
                parent_id=parent_id,
                seq=index,
                text=text,
                role="table_row_segment",
                fetch_mode=2,
                table_uid="t2",
                row_uid="r2",
                row_no=2,
                segment_index=index,
                segment_count=3,
            )
    kb_store.activate_version(doc_id, 1)
    base = {
        "doc_id": doc_id,
        "version": 1,
        "parent_id": parent_id,
        "fetch_mode": 2,
        "role": "table_row_segment",
        "table_uid": "t2",
        "row_uid": "r2",
        "segment_count": 3,
        "source_name": "a.txt",
        "heading": "标题",
        "loc": {"kind": "table", "table": "t2", "row_from": 2},
    }
    docs = [
        Document(
            page_content=f"段{index}",
            metadata={**base, "chunk_id": index + 1, "chunk_key": f"k{index}", "segment_index": index},
        )
        for index in range(3)
    ]
    candidates = compose_candidates(docs)
    assert len(candidates) == 1, "同一行的多个段必须归并成一个候选"
    meta = candidates[0].metadata
    assert meta["relation_ok"] is True
    assert meta["segment_indexes"] == [0, 1, 2]
    assert "地区: 华东" in meta["evidence_text"]
    assert "产品: A" in meta["evidence_text"]
    assert "金额: 1200" in meta["evidence_text"]
    assert build_evidence(candidates)[0]["relation_ok"] is True


def test_table_evidence_cap(monkeypatch):
    monkeypatch.setenv("HELPER_TABLE_EVIDENCE_CAP", "1")
    docs = [
        Document(
            page_content="row",
            metadata={
                "chunk_key": f"k{index}",
                "fetch_mode": 0,
                "table_uid": "t-cap",
                "evidence_text": f"行{index}",
                "relation_ok": True,
                "source_name": "a.txt",
            },
        )
        for index in range(3)
    ]
    evidence = build_evidence(docs, top_k=6)
    assert len(evidence) == 1, "同一张表只允许 cap 条证据"


def test_plain_table_gets_summary_and_labeled_rows(fake_root):
    _init(fake_root)
    splitter = Splitter(
        parent_size=200,
        parent_overlap=10,
        child_size=40,
        child_overlap=5,
        min_child_size=1,
    )
    block = Block(
        text="区域 | 金额\n华东 | 1200",
        content_type=TYPE_TABLE,
        header=("区域", "金额"),
        row_from=1,
        row_to=2,
    )
    groups = splitter.feed([block]) + splitter.flush()
    assert groups[0].parent.role == "table_parent"
    rows = [c for g in groups for c in g.children if c.role == "table_row"]
    assert len(rows) == 1
    assert rows[0].text == "区域: 华东 | 金额: 1200"
    assert rows[0].row_no == 2


def test_schema_has_relation_columns(fake_root):
    _init(fake_root)
    columns = {row[1] for row in get_connection().execute("PRAGMA table_info(chunks)")}
    for name in (
        "role",
        "fetch_mode",
        "table_uid",
        "row_uid",
        "segment_index",
        "segment_count",
        "merged_json",
    ):
        assert name in columns
