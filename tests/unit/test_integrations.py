"""Phase 1：integrations 适配层（sparse / embeddings / qdrant / llm）。"""

from __future__ import annotations

import pytest

from app.core.db import schema_v1
from app.core.db.connection import connect


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


def test_sparse_adapter_bm25(fake_root):
    from app.integrations.sparse import JiebaBm25SparseEmbeddings

    adapter = JiebaBm25SparseEmbeddings()
    docs = adapter.embed_documents(["合同违约金计算标准", "天气 北京"])
    assert len(docs) == 2
    query = adapter.embed_query("违约金")
    assert query.indices and query.values
    assert adapter.has_terms("违约金") is True
    assert adapter.has_terms("不存在的词xyz") is False


def test_sparse_term_store_stats(fake_root):
    from app.integrations.sparse import SparseTermStore

    store = SparseTermStore()
    store.register_documents([["合同", "违约金"], ["合同", "标准"]])
    assert store.n() == 2
    assert store.avgdl() == 2.0
    stats = store.term_stats(["合同"])
    assert stats["合同"][1] == 2  # df
    store.reset()
    assert store.n() == 0
    assert store.term_id("合同") is not None


def test_qdrant_single_collection_add_and_search(fake_root):
    import contextlib

    from langchain_core.documents import Document
    from langchain_core.embeddings import Embeddings
    from qdrant_client import QdrantClient, models

    from app.integrations import qdrant as qi

    class FakeEmbed(Embeddings):
        def embed_documents(self, texts):
            return [
                [1.0, 0.0, 0.0, 0.0] if "合同" in text else [0.0, 1.0, 0.0, 0.0]
                for text in texts
            ]

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    local = fake_root / "qdrant-local"
    local.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(local))
    try:
        write, dense, sparse = qi.build_stores(
            client, embedding=FakeEmbed(), dims=4
        )
        docs = [
            Document(
                page_content="合同违约金",
                metadata={"chunk_id": 1, "chunk_key": "1:1:1", "doc_id": 1,
                          "parent_id": 10, "status": "active", "source_name": "a.txt"},
            ),
            Document(
                page_content="北京天气",
                metadata={"chunk_id": 2, "chunk_key": "1:1:2", "doc_id": 1,
                          "parent_id": 10, "status": "active", "source_name": "a.txt"},
            ),
        ]
        write.add_documents(docs, ids=["00000000-0000-0000-0000-000000000001",
                                       "00000000-0000-0000-0000-000000000002"])

        active = models.Filter(
            must=[models.FieldCondition(key="metadata.status",
                                        match=models.MatchValue(value="active"))]
        )
        dense_hits = dense.similarity_search_with_score("合同", k=2, filter=active)
        sparse_hits = sparse.similarity_search_with_score("合同", k=2, filter=active)
        assert dense_hits, "dense 检索不应为空"
        assert sparse_hits, "sparse 检索不应为空"
        assert dense_hits[0][0].metadata["chunk_key"] == "1:1:1"
    finally:
        with contextlib.suppress(Exception):
            client.close()


def test_llm_factory(fake_root):
    from app.core.llm import store as slot_store
    from app.core.security import protect
    from app.integrations import llm as llm_integration

    assert llm_integration.chat_model() is None  # 没配置 → None

    slot_store.upsert_with_key(
        "llm",
        provider="openai_compatible",
        base_url="http://127.0.0.1:9/v1",
        model="test-model",
        api_key_cipher=protect(b"0123456789abcdef"),
        key_tail="cdef",
    )
    model = llm_integration.chat_model(temperature=0.0)
    assert model is not None
    assert model.model_name == "test-model"
    llm_integration.clear_model_cache()


def test_llm_base_url_normalized(fake_root):  # 端点误填完整路径时要纠正
    from app.integrations.llm import normalize_base_url  # 纯函数

    assert normalize_base_url("https://x/api/v3/chat/completions") == "https://x/api/v3"  # 去掉 chat 后缀
    assert normalize_base_url("https://x/api/v3/") == "https://x/api/v3"  # 去掉尾部斜杠
    assert normalize_base_url("https://x/api/v3") == "https://x/api/v3"  # 原样返回

