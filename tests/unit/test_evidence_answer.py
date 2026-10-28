"""Phase 4：证据组装 + 生成/引用校验（不依赖 LLM）。"""

from __future__ import annotations

import pytest
from langchain_core.documents import Document

from app.core.db import schema_v1
from app.core.db.connection import connect, transaction
from app.rag import kb_store
from app.rag.answer import extractive, validate_citations
from app.rag.evidence import build_evidence


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


def test_build_evidence_from_parents(fake_root):
    doc_id = kb_store.insert_document(name="a.txt", stored_path="a.txt", file_hash="f" * 64)
    with transaction() as conn:
        parent_id = kb_store.insert_parent(
            conn, doc_id=doc_id, version=1, text="父块正文",
            heading="合同说明", section=("合同说明",), loc={"kind": "page", "page": 3},
        )
        kb_store.insert_child(
            conn, doc_id=doc_id, version=1, parent_id=parent_id, seq=0,
            text="子块正文", heading="合同说明", section=("合同说明",),
            loc={"kind": "page", "page": 3},
        )
    kb_store.activate_version(doc_id, 1)

    doc = Document(
        page_content="子块正文",
        metadata={
            "chunk_id": 1, "chunk_key": "1:1:1", "doc_id": doc_id,
            "parent_id": parent_id, "source_name": "a.txt",
            "heading": "合同说明", "loc": {"kind": "page", "page": 3},
        },
    )
    evidence = build_evidence([doc], top_k=6)
    assert len(evidence) == 1
    assert evidence[0]["text"] == "父块正文\n\n子块正文"
    assert evidence[0]["origin"] == "合同说明 · 第 3 页"


def test_validate_citations():
    evidence = [{"index": 1}, {"index": 2}]
    clean, used = validate_citations("依据 [1] 与 [3] 说明 [2]", evidence)
    assert clean == "依据 [1] 与  说明 [2]"
    assert used == [1, 2]


def test_extractive_answer(fake_root):
    text = extractive([{"source_name": "a.txt", "origin": "第 1 页", "text": "正文"}])
    assert "正文" in text and "未改写" in text
