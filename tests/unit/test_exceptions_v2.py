"""异常路径矩阵（v2 净室）：DB / trace / 图 / 检索 / 生成 / 入库。"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from langchain_core.documents import Document

from app.core.db import schema_v1
from app.core.db.connection import connect, get_connection
from app.rag import kb_store


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


def _txt(fake_root, name="a.txt", data: bytes = b"hello world"):
    folder = fake_root / "src"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def test_schema_rejects_old_version(fake_root):
    conn = connect()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version','20')")
        with pytest.raises(ValueError, match="不兼容"):
            schema_v1.initialize(conn)
    finally:
        conn.close()


def test_schema_rejects_garbage_db(fake_root):
    path = fake_root / "garbage.db"
    path.write_bytes(b"this is not sqlite")
    conn = sqlite3.connect(str(path))
    try:
        with pytest.raises(sqlite3.DatabaseError):
            schema_v1.initialize(conn)
    finally:
        conn.close()


def test_trace_fail_open_without_tables(fake_root):
    from app.core.trace.recorder import trace_run, trace_span

    get_connection().executescript(
        "DROP TABLE IF EXISTS trace_events; DROP TABLE IF EXISTS trace_hits;"
        "DROP TABLE IF EXISTS trace_spans; DROP TABLE IF EXISTS trace_runs;"
    )
    ran = []
    with trace_run(question="q", conversation_id="c") as run:
        with trace_span("x", "node"):
            ran.append(True)
        run.event(code="x", message="y")
    assert ran == [True]  # trace 写失败不抛


def test_finalize_survives_missing_conversation_tables(fake_root):
    from app.agent.nodes.finalize.node import finalize_node

    get_connection().executescript(
        "DROP TABLE IF EXISTS messages; DROP TABLE IF EXISTS conversations;"
    )
    result = asyncio.run(
        finalize_node({"conversation_id": "c", "question": "q", "answer": "a"}, {})
    )
    assert result == {}  # 落库失败也不阻断（只告警）


def test_rerank_failure_returns_original_documents(monkeypatch):
    from app.integrations import rerank as rerank_mod

    def boom(*args, **kwargs):
        raise RuntimeError("onnx down")

    monkeypatch.setattr(rerank_mod, "score_pairs", boom)
    docs = [Document(page_content="a"), Document(page_content="b"), Document(page_content="c")]
    out = rerank_mod.OnnxReranker(top_n=2).compress_documents(docs, "q")
    assert [d.page_content for d in out] == ["a", "b"]


def test_rerank_threshold_filters_irrelevant(monkeypatch):
    from app.integrations import rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "is_worth_reranking", lambda count: True)
    monkeypatch.setattr(rerank_mod, "score_pairs", lambda q, docs: [5.0, -5.0])
    docs = [Document(page_content="related"), Document(page_content="noise")]
    out = rerank_mod.OnnxReranker(min_score=-1.0).compress_documents(docs, "q")
    assert [d.page_content for d in out] == ["related"]
    assert out[0].metadata["rerank_score"] == 5.0


def test_sparse_channel_without_terms_returns_empty():
    from app.integrations.retrievers import ChannelRetriever

    class Store:
        def similarity_search_with_score(self, *args, **kwargs):
            raise AssertionError("空查询不应访问 Qdrant")

    retriever = ChannelRetriever(
        store=Store(), stage="sparse", k=10, has_terms=lambda _text: False
    )
    assert retriever.invoke("完全不存在的词") == []


def test_ingest_unsupported_and_empty(fake_root):
    from app.ingest.pipeline import import_path

    bad = _txt(fake_root, "bad.exe", b"MZ")
    out = import_path(bad, writer=_NeverWriter())
    assert out.status == "failed" and out.reason

    empty = _txt(fake_root, "empty.txt", b"")
    out2 = import_path(empty, writer=_NeverWriter())
    assert out2.status == "failed"


def test_ingest_writer_failure_rolls_back(fake_root):
    from app.ingest.pipeline import import_path

    path = _txt(fake_root)
    out = import_path(path, writer=_FailingWriter())
    assert out.status == "failed"
    doc = kb_store.get_document(out.doc_id)
    assert doc is not None and doc["status"] == "failed"
    assert kb_store.counts()["chunks"] == 0



def test_trace_service_without_tables_raises(fake_root):
    from app.services import trace as trace_service

    get_connection().executescript(
        "DROP TABLE IF EXISTS trace_hits; DROP TABLE IF EXISTS trace_spans;"
        "DROP TABLE IF EXISTS trace_runs;"
    )
    with pytest.raises(sqlite3.OperationalError):
        trace_service.page(days=1)


def test_config_corrupt_setting_falls_back(fake_root):
    from app.core.config import store as params_store

    get_connection().execute(
        "INSERT OR REPLACE INTO settings(key, value_json, updated_at)"
        " VALUES ('chunk.semantic_fallback','{bad',0)"
    )
    config = params_store.load()
    assert config["chunk.semantic_fallback"] is True  # 坏 JSON 不炸，回默认


def test_empty_qdrant_returns_no_hits(fake_root):
    import contextlib

    from langchain_core.embeddings import Embeddings
    from qdrant_client import QdrantClient

    from app.integrations.qdrant import build_stores
    from app.integrations.retrievers import ChannelRetriever

    class FakeEmbed(Embeddings):
        def embed_documents(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    local = fake_root / "empty-qdrant"
    local.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(local))
    try:
        _write, dense, _sparse = build_stores(client, embedding=FakeEmbed(), dims=4)
        retriever = ChannelRetriever(store=dense, stage="dense", k=5)
        assert retriever.invoke("任何问题") == []
    finally:
        with contextlib.suppress(Exception):
            client.close()


class _NeverWriter:
    def add(self, items, ids, metas):
        raise AssertionError("不应调用 writer.add")

    def activate(self, doc_id, version):
        raise AssertionError("不应调用 writer.activate")

    def delete_version(self, doc_id, version):
        raise AssertionError("不应调用 writer.delete_version")


class _FailingWriter:
    def add(self, items, ids, metas):
        raise RuntimeError("qdrant write failed")

    def activate(self, doc_id, version):
        return None

    def delete_version(self, doc_id, version):
        return None
