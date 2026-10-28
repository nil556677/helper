"""v1 SQLite 知识库存储：documents/chunks 生命周期。"""

from __future__ import annotations

import pytest

from app.core.db import schema_v1
from app.core.db.connection import connect, transaction
from app.rag import kb_store


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


def test_document_and_chunks_lifecycle(fake_root):
    doc_id = kb_store.insert_document(
        name="a.txt",
        stored_path="abc/a.txt",
        file_hash="f" * 64,
        text_hash="t" * 64,
        size=12,
        doc_title="A",
    )
    assert doc_id > 0
    assert kb_store.find_by_file_hash("f" * 64)["id"] == doc_id
    assert kb_store.find_by_text_hash("t" * 64)["id"] == doc_id

    with transaction() as conn:
        parent_id = kb_store.insert_parent(
            conn,
            doc_id=doc_id,
            version=1,
            text="父块正文",
            heading="A › 1",
            section=("A", "1"),
            loc={"kind": "page", "page": 1},
            token_count=4,
        )
        child_id = kb_store.insert_child(
            conn,
            doc_id=doc_id,
            version=1,
            parent_id=parent_id,
            seq=0,
            text="子块正文",
            heading="A › 1",
            section=("A", "1"),
            loc={"kind": "page", "page": 1},
            token_count=4,
        )
    assert parent_id and child_id and child_id != parent_id
    assert kb_store.counts() == {"documents": 1, "chunks": 2, "parents": 1, "active": 0}

    parents = kb_store.parents_for([parent_id])
    assert parents[parent_id]["text"] == "父块正文"

    kb_store.activate_version(doc_id, 1, pages=3)
    doc = kb_store.get_document(doc_id)
    assert doc["status"] == "indexed"
    assert doc["pages"] == 3
    assert kb_store.counts()["active"] == 2

    assert kb_store.delete_version(doc_id, 1) == 2
    assert kb_store.get_document(doc_id) is not None
    kb_store.delete_document(doc_id)
    assert kb_store.get_document(doc_id) is None


def test_mark_failed(fake_root):
    doc_id = kb_store.insert_document(
        name="b.txt", stored_path="b.txt", file_hash="g" * 64
    )
    kb_store.mark_failed(doc_id, kind="bad_request", detail="坏文件")
    row = kb_store.get_document(doc_id)
    assert row["status"] == "failed"
    assert row["error_kind"] == "bad_request"
