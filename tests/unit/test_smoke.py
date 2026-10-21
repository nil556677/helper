"""测试装置冒烟：fake_root / rag_db 可用。"""

from __future__ import annotations


def test_ingest_db_initializes(ingest_db: None) -> None:
    from app.core.db.connection import get_connection
    from app.core.db.schema import schema_version

    assert schema_version(get_connection()) >= 11


def test_rag_db_writes_document(rag_db: None) -> None:
    from app.core.db.connection import transaction
    from app.rag import store

    with transaction() as conn:
        doc_id = store.insert_ready(
            conn,
            source_name="冒烟.md",
            stored_path="abc/冒烟.md",
            file_hash="x",
            text_hash=None,
            size=3,
            detail=None,
            now=1.0,
        )
    assert store.get(doc_id) is not None
