"""Phase 6：知识库 staging / lifecycle 切到 v1。"""

from __future__ import annotations

import pytest

from app.core.db import schema_v1
from app.core.db.connection import connect
from app.rag import kb_store
from app.services.knowledge import lifecycle, staging


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


def _file(fake_root, name="a.txt", data=b"hello"):
    folder = fake_root / "inbox"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def test_register_staged_and_duplicate(fake_root):
    path = _file(fake_root)
    payload = staging.register_staged("a.txt", path)
    assert payload["doc_id"]
    row = kb_store.get_document(int(payload["doc_id"]))
    assert row["status"] == "pending"
    assert row["stored_path"] == ""

    again = staging.register_staged("a.txt", path)
    assert again.get("skip") == payload["doc_id"]

    staging.discard_pending(payload["doc_id"])
    assert kb_store.get_document(int(payload["doc_id"])) is None


def test_delete_removes_document(fake_root, monkeypatch):
    doc_id = kb_store.insert_document(name="a.txt", stored_path="a/a.txt", file_hash="f" * 64)
    copy = fake_root / "data" / "files" / "a" / "a.txt"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text("hello", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "app.integrations.qdrant.delete_document_points",
        lambda client, target: calls.append(target),
    )
    monkeypatch.setattr("app.integrations.qdrant.qdrant_client", lambda: object())
    assert lifecycle.delete([doc_id]) == 1
    assert kb_store.get_document(doc_id) is None
    assert calls == [doc_id]
    assert not copy.exists(), "删除资料必须同时删副本"


def test_rebuild_uses_import_stored(fake_root, monkeypatch):
    doc_id = kb_store.insert_document(name="a.txt", stored_path="a/a.txt", file_hash="f" * 64)
    copy = fake_root / "data" / "files" / "a" / "a.txt"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text("hello", encoding="utf-8")
    # stored_path 是相对 data/files 的路径
    kb_store.update_document(doc_id, stored_path="a/a.txt")

    from types import SimpleNamespace

    seen = {}

    def fake_import(path, name=None, claim_doc_id=None):
        seen.update(path=str(path), name=name, claim=claim_doc_id)
        return SimpleNamespace(status="imported", reason="", chunks=3)

    monkeypatch.setattr("app.ingest.import_stored", fake_import)
    result = lifecycle.rebuild(doc_id)
    assert result.ok is True and result.chunks == 3
    assert seen["claim"] == doc_id
