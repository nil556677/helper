"""Qdrant 单集合（净室 v2）：**只支持服务端模式**。

- helper 启动时拉起 ``bin/qdrant/qdrant.exe --storage-path data/qdrant-server``；
- 只维护一个集合 ``kb_chunks``：dense（768, cosine）+ sparse（BM25）；
- 写用 ``RetrievalMode.HYBRID``（一次 upsert 双向量）；
- 读用 ``DENSE`` / ``SPARSE`` 两个实例分开查（后台要看每一路，不能只拿 RRF 融合结果）；
- 父块不进 Qdrant（SQLite 回填）。

本地嵌入式 ``data/qdrant`` 已移除；没有 ``vector.url`` 时默认连
``http://127.0.0.1:6333``（由 helper 自己拉起 / 或外部服务端）。
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

from app.core.config import store as params_store
from app.integrations.embeddings import OnnxBgeM3Embeddings
from app.integrations.sparse import JiebaBm25SparseEmbeddings

COLLECTION = "kb_chunks"
DENSE = "dense"
SPARSE = "sparse"
DENSE_DIMS = 768
DEFAULT_URL = "http://127.0.0.1:6333"

_lock = threading.Lock()
_client: Any = None
_client_url: str | None = None


def qdrant_client(*, url: str | None = None, api_key: str | None = None) -> Any:
    """取（必要时打开）服务端 client 单例；未配置时默认 ``127.0.0.1:6333``。"""
    global _client, _client_url
    config = params_store.load()
    cfg_url = str(config["vector.url"] or "").strip()
    cfg_key = str(config["vector.api_key"] or "").strip()
    target = (url or cfg_url or DEFAULT_URL).strip()
    if api_key is None and cfg_key:
        api_key = cfg_key

    with _lock:
        if _client is not None and _client_url == target:
            return _client
        if _client is not None:
            with contextlib.suppress(Exception):
                _client.close()
            _client = None
        from qdrant_client import QdrantClient

        kwargs: dict[str, Any] = {"url": target, "timeout": 10.0, "check_compatibility": False}
        if api_key:
            kwargs["api_key"] = api_key
        _client = QdrantClient(**kwargs)
        _client_url = target
        return _client


def ensure_collection(client: Any | None = None, *, dims: int = DENSE_DIMS) -> None:
    """保证 ``kb_chunks`` 存在（幂等）+ payload 索引。"""
    client = client or qdrant_client()
    from qdrant_client import models

    if client.collection_exists(COLLECTION):
        return
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={
            DENSE: models.VectorParams(size=dims, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={SPARSE: models.SparseVectorParams()},
    )
    for field, schema in (
        ("metadata.doc_id", models.PayloadSchemaType.INTEGER),
        ("metadata.version", models.PayloadSchemaType.INTEGER),
        ("metadata.status", models.PayloadSchemaType.KEYWORD),
        ("metadata.chunk_key", models.PayloadSchemaType.KEYWORD),
        ("metadata.role", models.PayloadSchemaType.KEYWORD),
        ("metadata.fetch_mode", models.PayloadSchemaType.INTEGER),
        ("metadata.table_uid", models.PayloadSchemaType.KEYWORD),
        ("metadata.row_uid", models.PayloadSchemaType.KEYWORD),
        ("metadata.segment_index", models.PayloadSchemaType.INTEGER),
        ("status", models.PayloadSchemaType.KEYWORD),
    ):
        client.create_payload_index(
            collection_name=COLLECTION,
            field_name=field,
            field_schema=schema,
        )


def build_stores(
    client: Any | None = None,
    *,
    embedding: Any | None = None,
    sparse_embedding: Any | None = None,
    dims: int = DENSE_DIMS,
) -> tuple[Any, Any, Any]:
    """返回 ``(write_store, dense_store, sparse_store)``。"""
    client = client or qdrant_client()
    embedding = embedding or OnnxBgeM3Embeddings()
    sparse_embedding = sparse_embedding or JiebaBm25SparseEmbeddings()
    ensure_collection(client, dims=dims)

    from langchain_qdrant import QdrantVectorStore, RetrievalMode

    write_store = QdrantVectorStore(
        client=client,
        collection_name=COLLECTION,
        embedding=embedding,
        sparse_embedding=sparse_embedding,
        retrieval_mode=RetrievalMode.HYBRID,
        vector_name=DENSE,
        sparse_vector_name=SPARSE,
    )
    dense_store = QdrantVectorStore(
        client=client,
        collection_name=COLLECTION,
        embedding=embedding,
        retrieval_mode=RetrievalMode.DENSE,
        vector_name=DENSE,
    )
    sparse_store = QdrantVectorStore(
        client=client,
        collection_name=COLLECTION,
        embedding=None,
        sparse_embedding=sparse_embedding,
        retrieval_mode=RetrievalMode.SPARSE,
        sparse_vector_name=SPARSE,
    )
    return write_store, dense_store, sparse_store


def reset_collection(client: Any | None = None) -> None:
    """删集合并重建（嵌入维度/模型变化时只做这一件事，没有 alias）。"""
    client = client or qdrant_client()
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    ensure_collection(client)


def close_client() -> None:
    """关闭进程级 client（退出时用，避免解释器卸载阶段再触发 __del__）。"""
    global _client, _client_url
    with _lock:
        if _client is not None:
            with contextlib.suppress(Exception):
                _client.close()
        _client = None
        _client_url = None


def delete_document_points(client: Any, doc_id: int) -> None:
    """按 metadata.doc_id 删除一份资料的全部点（所有版本）。"""
    from qdrant_client import models

    client.delete(
        collection_name=COLLECTION,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="metadata.doc_id",
                        match=models.MatchValue(value=int(doc_id)),
                    )
                ]
            )
        ),
    )
