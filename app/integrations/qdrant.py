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
from app.core.retry import RetryPolicy, retry_sync  # 同步重试
from app.integrations.embeddings import OnnxBgeM3Embeddings
from app.integrations.sparse import JiebaBm25SparseEmbeddings

#: 重试策略：只重试瞬时类（断连/限流/5xx）。
_RETRY = RetryPolicy(max_retries=3, base_delay_s=0.5, max_delay_s=4.0, jitter_s=0.1)  # 策略

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


def _wanted_sparse_config() -> dict[str, Any]:  # 期望的稀疏向量配置
    """单 ``sparse``（兼容旧 store）+ 每字段命名稀疏向量。"""
    from qdrant_client import models  # 向量参数

    from app.core.sparse import fields as kw_fields  # 字段名

    cfg: dict[str, Any] = {SPARSE: models.SparseVectorParams()}  # 兼容单 sparse
    for field in kw_fields.INDEX_FIELDS:  # 每个索引字段
        cfg[kw_fields.sparse_vector_name(field)] = models.SparseVectorParams()  # 命名稀疏
    return cfg  # 返回


def _create_collection_with_indexes(client: Any, *, dims: int) -> None:  # 新建集合 + payload 索引
    """建 ``kb_chunks``（dense + sparse + 每字段命名稀疏）并建 payload 索引。"""
    from qdrant_client import models

    client.create_collection(  # 建集合
        collection_name=COLLECTION,  # 集合
        vectors_config={  # dense
            DENSE: models.VectorParams(size=dims, distance=models.Distance.COSINE)  # dense 参数
        },  # dense 结束
        sparse_vectors_config=_wanted_sparse_config(),  # 稀疏（single + 每字段）
    )  # 建集合结束
    for field, schema in (  # payload 索引
        ("metadata.doc_id", models.PayloadSchemaType.INTEGER),  # 文档
        ("metadata.version", models.PayloadSchemaType.INTEGER),  # 版本
        ("metadata.status", models.PayloadSchemaType.KEYWORD),  # 状态
        ("metadata.chunk_key", models.PayloadSchemaType.KEYWORD),  # 块键
        ("metadata.role", models.PayloadSchemaType.KEYWORD),  # 角色
        ("metadata.fetch_mode", models.PayloadSchemaType.INTEGER),  # 取数模式
        ("metadata.table_uid", models.PayloadSchemaType.KEYWORD),  # 表
        ("metadata.row_uid", models.PayloadSchemaType.KEYWORD),  # 行
        ("metadata.segment_index", models.PayloadSchemaType.INTEGER),  # 段位
        ("status", models.PayloadSchemaType.KEYWORD),  # 顶层状态
    ):  # 遍历
        client.create_payload_index(  # 建索引
            collection_name=COLLECTION,  # 集合
            field_name=field,  # 字段
            field_schema=schema,  # 类型
        )  # 建索引结束


def ensure_collection(client: Any | None = None, *, dims: int = DENSE_DIMS) -> None:
    """保证 ``kb_chunks`` 具有期望的稀疏向量（幂等）。

    不存在则新建；已存在但缺期望的命名稀疏向量时**重建**（本 Qdrant 版本无法增量加命名
    稀疏向量）。重建会清空集合数据——多字段升级期的既定行为（数据由部署方重灌）。
    """
    client = client or qdrant_client()
    if not client.collection_exists(COLLECTION):
        _create_collection_with_indexes(client, dims=dims)  # 新建
        return
    # 已存在：核对稀疏向量是否齐全（本版本 update_collection 不能加命名稀疏 → 缺则重建）
    existing = client.get_collection(COLLECTION).config.params.sparse_vectors  # 现有稀疏向量
    existing = existing or {}  # 兼容 None
    wanted = _wanted_sparse_config()  # 期望
    missing = {name for name in wanted if str(name) not in existing}  # 缺失的命名向量
    if not missing:  # 已齐
        return  # 无事
    from loguru import logger  # 日志

    logger.warning(  # 留痕
        "稀疏向量缺 {}，重建集合 {}（旧数据清空，由部署方重灌）", sorted(missing), COLLECTION  # 信息
    )
    client.delete_collection(COLLECTION)  # 删（旧数据弃用）
    _create_collection_with_indexes(client, dims=dims)  # 重建新 schema


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
    """按 metadata.doc_id 删除一份资料的全部点（所有版本）；幂等，安全重试。"""
    from qdrant_client import models

    retry_sync(  # 幂等 delete，可重试
        lambda: client.delete(  # 删
            collection_name=COLLECTION,  # 集合
            points_selector=models.FilterSelector(  # 按过滤删
                filter=models.Filter(  # 过滤
                    must=[  # 必须满足
                        models.FieldCondition(  # 字段条件
                            key="metadata.doc_id",  # 文档字段
                            match=models.MatchValue(value=int(doc_id)),  # 文档 id
                        )  # 条件结束
                    ]  # 必须结束
                )  # 过滤结束
            ),  # 删除选择器结束
        ),  # 删结束
        _RETRY,  # 重试策略
    )  # 重试结束
