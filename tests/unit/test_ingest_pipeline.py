"""Phase 2：净室入库流水线（SQLite chunks + 注入 writer）。"""

from __future__ import annotations

import pytest

from app.core.db import schema_v1
from app.core.db.connection import connect
from app.rag import kb_store


class FakeWriter:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.activated: list[tuple[int, int]] = []
        self.deleted: list[tuple[int, int]] = []

    def add(self, items, ids, metas) -> None:
        assert len(items) == len(ids) == len(metas)
        self.added.extend(items)

    def activate(self, doc_id: int, version: int) -> None:
        self.activated.append((doc_id, version))

    def delete_version(self, doc_id: int, version: int) -> None:
        self.deleted.append((doc_id, version))


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


def _txt(fake_root, name: str = "sample.txt", text: str | None = None):
    folder = fake_root / "src"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(
        text
        or (
            "# 合同说明\n\n"
            "第一段：合同违约金按未付款项的每日万分之五计算。\n\n"
            "第二段：争议提交北京仲裁委员会解决。\n"
        ),
        encoding="utf-8",
    )
    return path


def test_import_txt_writes_sqlite_and_writer(fake_root):
    from app.ingest.pipeline import import_path

    path = _txt(fake_root)
    writer = FakeWriter()
    outcome = import_path(path, writer=writer)

    assert outcome.status == "imported", outcome.reason
    assert outcome.doc_id
    assert outcome.chunks >= 1
    assert len(writer.added) == outcome.chunks
    assert writer.activated == [(outcome.doc_id, 1)]

    doc = kb_store.get_document(outcome.doc_id)
    assert doc["status"] == "indexed"
    assert doc["text_hash"]
    counts = kb_store.counts()
    assert counts["parents"] >= 1
    assert counts["active"] == outcome.chunks + counts["parents"]


def test_import_same_file_skipped(fake_root):
    from app.ingest.pipeline import import_path

    path = _txt(fake_root)
    first = import_path(path, writer=FakeWriter())
    assert first.status == "imported"
    second = import_path(path, writer=FakeWriter())
    assert second.status == "skipped"
    assert second.doc_id == first.doc_id


def test_rebuild_same_doc_not_skipped_by_hash(fake_root):
    from app.ingest.pipeline import import_path

    path = _txt(fake_root)
    first = import_path(path, writer=FakeWriter())
    assert first.status == "imported"
    rebuilt = import_path(path, writer=FakeWriter(), claim_doc_id=first.doc_id)
    assert rebuilt.status == "imported"
    assert rebuilt.doc_id == first.doc_id
    assert int(kb_store.get_document(first.doc_id)["version"]) == 2
    assert kb_store.counts()["documents"] == 1


def test_qdrant_writer_end_to_end(fake_root):
    import contextlib

    from langchain_core.embeddings import Embeddings
    from qdrant_client import QdrantClient, models

    from app.ingest.pipeline import import_path
    from app.ingest.qdrant_writer import QdrantChunkWriter
    from app.integrations.qdrant import COLLECTION, ensure_collection

    class FakeEmbed(Embeddings):
        def embed_documents(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    local = fake_root / "qdrant-e2e"
    local.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(local))
    try:
        ensure_collection(client, dims=4)
        writer = QdrantChunkWriter(client=client, embedding=FakeEmbed(), dims=4)
        outcome = import_path(_txt(fake_root), writer=writer)
        assert outcome.status == "imported", outcome.reason

        filt = models.Filter(
            must=[
                models.FieldCondition(
                    key="metadata.doc_id",
                    match=models.MatchValue(value=int(outcome.doc_id)),
                ),
                models.FieldCondition(
                    key="status", match=models.MatchValue(value="active")
                ),
            ]
        )
        count = client.count(collection_name=COLLECTION, count_filter=filt).count
        assert count == outcome.chunks
        assert count > 0
    finally:
        with contextlib.suppress(Exception):
            client.close()

