"""Phase 4：dense/sparse 两路召回 + RRF（真实 Qdrant + 假嵌入）。"""

from __future__ import annotations

import contextlib

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from qdrant_client import QdrantClient

from app.core.db import schema_v1
from app.core.db.connection import connect
from app.integrations.qdrant import COLLECTION, ensure_collection
from app.integrations.retrievers import ChannelRetriever, HybridRetriever
from app.integrations.sparse import JiebaBm25SparseEmbeddings


class FakeEmbed(Embeddings):
    def embed_documents(self, texts):
        return [
            [1.0, 0.0, 0.0, 0.0] if "合同" in text else [0.0, 1.0, 0.0, 0.0]
            for text in texts
        ]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


@pytest.fixture
def stores(fake_root):
    from app.integrations.qdrant import build_stores

    local = fake_root / "qdrant-ret"
    local.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(local))
    sparse_emb = JiebaBm25SparseEmbeddings()
    ensure_collection(client, dims=4)
    write, dense, sparse = build_stores(client, embedding=FakeEmbed(), sparse_embedding=sparse_emb, dims=4)
    try:
        docs = [
            Document(
                page_content="合同违约金计算标准",
                metadata={"chunk_id": 1, "chunk_key": "1:1:1", "doc_id": 1,
                          "parent_id": 10, "version": 1, "status": "pending",
                          "source_name": "a.txt", "heading": "合同说明"},
            ),
            Document(
                page_content="北京天气",
                metadata={"chunk_id": 2, "chunk_key": "1:1:2", "doc_id": 1,
                          "parent_id": 10, "version": 1, "status": "pending",
                          "source_name": "a.txt", "heading": "天气"},
            ),
        ]
        ids = ["00000000-0000-0000-0000-000000000001",
               "00000000-0000-0000-0000-000000000002"]
        write.add_documents(docs, ids=ids)
        client.set_payload(COLLECTION, payload={"status": "active"}, points=ids)
        yield client, dense, sparse, sparse_emb
    finally:
        with contextlib.suppress(Exception):
            client.close()


def test_channel_and_hybrid_retrieval(stores):
    _client, dense, sparse, sparse_emb = stores
    dense_retriever = ChannelRetriever(store=dense, stage="dense", k=40)
    sparse_retriever = ChannelRetriever(
        store=sparse, stage="sparse", k=50, has_terms=sparse_emb.has_terms
    )
    hybrid = HybridRetriever(dense=dense_retriever, sparse=sparse_retriever, top_k=50, rrf_k=60)

    dense_docs = dense_retriever.invoke("合同")
    assert dense_docs and dense_docs[0].metadata["chunk_key"] == "1:1:1"

    sparse_docs = sparse_retriever.invoke("合同")
    assert sparse_docs, "sparse 应命中登记的词"

    fused = hybrid.invoke("合同")
    assert fused and fused[0].metadata["chunk_key"] == "1:1:1"
