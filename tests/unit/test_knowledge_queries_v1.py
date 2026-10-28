"""Phase 6：知识库查询层切到 v1（SQLite）。"""

from __future__ import annotations

import pytest

from app.core.db import schema_v1
from app.core.db.connection import connect, transaction
from app.rag import kb_store
from app.services.knowledge import queries


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


def _seed():
    doc_id = kb_store.insert_document(
        name="合同.txt", stored_path="abc/合同.txt", file_hash="f" * 64,
        size=123, doc_title="合同", status="pending",
    )
    with transaction() as conn:
        parent_id = kb_store.insert_parent(
            conn, doc_id=doc_id, version=1, text="父块正文",
            heading="合同 › 违约金", section=("合同", "违约金"),
            loc={"kind": "page", "page": 2},
        )
        kb_store.insert_child(
            conn, doc_id=doc_id, version=1, parent_id=parent_id, seq=0,
            text="子块正文", heading="合同 › 违约金", section=("合同", "违约金"),
            loc={"kind": "page", "page": 2}, kind="text",
        )
    kb_store.activate_version(doc_id, 1, pages=2)
    return doc_id


def test_stats_and_page(fake_root):
    doc_id = _seed()
    st = queries.stats()
    assert st.documents == 1 and st.chunks == 1 and st.vectors_done == 1

    page = queries.documents_page(page=1, size=20)
    assert page.total == 1
    item = page.items[0]
    assert item.id == doc_id and item.name == "合同.txt" and item.chunks == 1
    assert item.status == "indexed"


def test_detail_and_chunks(fake_root):
    doc_id = _seed()
    detail = queries.document_detail(doc_id)
    assert detail is not None
    assert detail["name"] == "合同.txt"
    assert detail["children"] == 1 and detail["parents"] == 1

    chunks = queries.document_chunks(doc_id)
    assert chunks["total"] == 1
    item = chunks["items"][0]
    assert item["text"] == "子块正文"
    assert item["parent_text"] == "父块正文"
    assert item["loc"]["page"] == 2

    assert queries.document_chunks(doc_id, heading="违约金")["total"] == 1
    assert queries.document_chunks(doc_id, heading="无此标题")["total"] == 0


def test_preview_and_codes(fake_root):
    doc_id = _seed()
    preview = queries.document_preview(doc_id)
    assert preview["found"] is True
    # 副本没复制到 fake_root，所以提示"副本不在了"，但不崩
    assert preview["text"] == ""
    assert queries.document_codes(doc_id) == []
