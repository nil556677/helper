"""向量层的对外接口：**Qdrant 是唯一向量后端与检索来源**（docs/向量层重构设计.md）。

移除进程内矩阵打分缓存后，这里只编排 Qdrant。调用方只认这里的函数，
不关心向量存在哪（技术方案 §9.4 汇总；细节看重构设计）：

```python
api.ensure(COLLECTION_CHUNKS, dims=768)        # 建集合 + payload 索引（幂等）
api.upsert(COLLECTION_CHUNKS, entries)         # 写向量（Qdrant）
api.search(COLLECTION_CHUNKS, query_vector, top_k=50, where={"status": "active"})
api.count(COLLECTION_CHUNKS) / api.stats()     # 对账与界面占用
```

**连接模式**由 ``vector.url`` 决定（见 `qdrant_store.client()`）：
- 配了 ``vector.url`` → 连服务端（HNSW / payload 索引生效）；
- 留空 → 本地嵌入式（无 HNSW，仅开发/测试）。

**过滤**统一在 Qdrant 侧做（payload 过滤 + 先筛再查）；不再有"矩阵掩码"那套。
"""

from __future__ import annotations

from typing import Any

import numpy as np
from loguru import logger

from app.core.config import store as params_store
from app.core.vector import qdrant_store
from app.core.vector.collections import (
    ALL as ALL_COLLECTIONS,
)
from app.core.vector.collections import (
    MEMORY,
    RAG_CHUNKS,
    RAG_PARENTS,
)

#: 集合名（**带领域前缀**；注册表见 :mod:`app.core.vector.collections`）。
COLLECTION_CHUNKS = RAG_CHUNKS.name
COLLECTION_PARENTS = RAG_PARENTS.name
COLLECTION_MEMORY = MEMORY.name


#: 一条向量的标识：`{"key": "doc:version:seq", "vector": ndarray, "payload": {...}}`。
Entry = dict[str, Any]


class VectorError(RuntimeError):
    """向量层不可用（后端没装 / 维度不符 / 写入失败）。"""


def dims() -> int:
    """当前配置的向量维度。"""
    return int(params_store.load()["embed.dims"])


def backend() -> str:
    """向量后端名（恒定 ``qdrant``）。"""
    return "qdrant"


def ensure(collection: str, *, dims_: int | None = None) -> dict[str, Any]:
    """保证集合可用（建集合 + 固定 payload 索引）。幂等。

    参数：
        collection: 集合名。
        dims_: 维度；默认取配置。
    返回：``{"backend", "collection", "created"}``。
    """
    size = int(dims_ or dims())
    collection = collection
    created = qdrant_store.ensure_collection(collection, size)
    # 索引早于数据写入建立，否则 HNSW 的 filterable 边不带上这些字段。
    qdrant_store.ensure_payload_indexes(collection)
    return {"backend": "qdrant", "collection": collection, "created": created}


def ensure_parents() -> bool:
    """保证 parent 的无向量集合存在；返回是否新建。"""
    return qdrant_store.ensure_vectorless_collection(COLLECTION_PARENTS)


def upsert(collection: str, entries: list[Entry], *, dims_: int | None = None) -> int:
    """写入一批向量（只写 Qdrant）。

    返回：写了几条。
    异常：
        VectorError: 维度不符或写入失败。
    """
    if not entries:
        return 0
    with VectorSession(collection, dims_=dims_) as session:
        count = session.add(entries)
        session.flush()
        return count


def delete_keys(collection: str, keys: list[str]) -> dict[str, int]:
    """按 key 删向量（Qdrant）。P1.9 清理旧版本用。

    返回：``{"qdrant": 删了几条}``。
    """
    if not keys:
        return {"qdrant": 0}
    deleted = qdrant_store.delete_points(
        collection, [qdrant_store.stable_point_id(key) for key in keys]
    )
    return {"qdrant": deleted}


def delete_where(collection: str, where: dict[str, object]) -> dict[str, int]:
    """按 **payload 过滤**删点（服务端筛选，调用方不必先 scroll 预扫描）。

    删整份资料用它（``{"documents_id": doc_id}``）——点数与版本都无需预知。
    返回：``{"qdrant": 删了几条}``。
    """
    return {"qdrant": qdrant_store.delete_points_where(collection, where)}


def delete_points(collection: str, ids: list[int]) -> int:
    """按 **point id** 删点（对账/自愈里"删孤儿点"用；内部按 1000 分批）。

    与 :func:`delete_keys` 的区别：这里拿的是 scroll 出来的数字 id，**不再做
    key→id 的哈希换算**（老数据没有 ``payload.key`` 时只能走这条路）。
    返回：请求删除的条数。
    """
    if not ids:
        return 0
    return qdrant_store.delete_points(collection, [int(i) for i in ids])


def iter_points(collection: str, where: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """遍历集合里的全部点（对账/自愈用）：``{"id": int, "payload": dict}`` 列表。

    一次性拉全量（本地模式本就是全内存；服务端走分页 scroll 拼接）。
    """
    return [
        {"id": int(point.id), "payload": dict(point.payload or {})}
        for point in qdrant_store.scroll_filter(collection, where)
    ]


def set_payload(collection: str, keys: list[str], patch: dict[str, Any]) -> dict[str, int]:
    """把 payload 补丁打到一批 key 上（版本切换：旧版 superseded、新版 active）。"""
    return set_payloads(collection, [(keys, patch)])


def set_payloads(
    collection: str, groups: list[tuple[list[str], dict[str, Any]]]
) -> dict[str, int]:
    """一次给多组 key 打不同补丁。返回 ``{"qdrant": 写了几条}``。"""
    written = 0
    for keys, patch in groups:
        if not keys:
            continue
        written += qdrant_store.set_payload(
            collection, [qdrant_store.stable_point_id(key) for key in keys], patch
        )
    return {"qdrant": written}


class VectorSession:
    """分批写入向量的会话：**攒一批写一次 Qdrant**。

    保留这个类是为了让调用方（embedding）不感知"如何批写"。与旧矩阵版本不同，
    这里没有矩阵：写是真实吞进 Qdrant，崩溃最多丢最后一批（可续跑）。

    用法（``with`` 保证退出时 flush）：

    ```python
    with VectorSession(COLLECTION_CHUNKS) as session:
        for batch in batches:
            session.add([...])
            session.flush_if_needed()
        session.flush()
    ```
    """

    def __init__(
        self, collection: str, *, dims_: int | None = None, batch_size: int = 2000
    ) -> None:
        self.collection = collection
        self.size = int(dims_ or dims())
        self.batch_size = max(1, int(batch_size)) if batch_size else 0
        self._pending: list[tuple[str, np.ndarray, dict[str, Any]]] = []
        self.added = 0
        self.written = 0

    def __enter__(self) -> VectorSession:
        return self

    def __exit__(self, *exc: object) -> None:
        if exc[0] is None:
            # 无异常才 flush：异常路径下重写同一批点既掩盖原始错误又重复写（异常审查）
            self.flush()
        self.close()

    @property
    def count(self) -> int:
        """已写入的条数。"""
        return self.added

    @property
    def keys(self) -> list[str]:
        return [key for key, _, _ in self._pending]

    def add(self, entries: list[Entry]) -> int:
        """入一批（校验维度后攒进内存，超阈值则落一次盘）。"""
        if not entries:
            return 0
        items: list[tuple[str, np.ndarray, dict[str, Any]]] = []
        for item in entries:
            vector_in = item.get("vector")
            if vector_in is None:
                # 只有向量集合才走这里；无向量集合（如 parent）另走 vectorless 路径。
                # None 直接喂 numpy 会抛难懂的 TypeError，这里给个可分类的错。
                raise VectorError(
                    "写入向量集合的记录缺少 vector（无向量集合请走 vectorless 写入路径）"
                )
            vector = np.asarray(vector_in, dtype=np.float32).reshape(-1)
            if vector.shape[0] != self.size:
                raise VectorError(
                    f"向量维度 {vector.shape[0]} 与配置的 {self.size} 不一致"
                )
            items.append(
                (str(item["key"]), vector, dict(item.get("payload") or {}))
            )
        self._pending.extend(items)
        self.added += len(items)
        self.flush_if_needed()
        return len(items)

    def flush_if_needed(self) -> bool:
        if self.batch_size and len(self._pending) >= self.batch_size:
            self.flush()
            return True
        return False

    def flush(self) -> None:
        if not self._pending:
            return
        keys = [k for k, _, _ in self._pending]
        vectors = [v for _, v, _ in self._pending]
        payloads = [p for _, _, p in self._pending]
        self.written += qdrant_store.write_vectors(
            self.collection, self.size, list(zip(keys, vectors, payloads, strict=True))
        )
        self._pending = []

    def close(self) -> None:
        self._pending = []


def search(
    collection: str,
    vector: np.ndarray,
    *,
    top_k: int = 50,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """找最相似的若干条（**固定走 Qdrant**）。

    返回：``[{"key", "score", "payload"}, ...]``，按分数降序。
    payload 含 key 之外的字段（status/documents_id/text/parent_key…），供上层回填。
    """
    hits = qdrant_store.query(
        collection, vector, limit=top_k, where=where, dims=dims()
    )
    return [
        {
            # payload 写入时存了 key；没有就退回 id 的数字形式（老数据）。
            "key": str(hit["payload"].get("key") or hit["id"]),
            "score": hit["score"],
            "payload": {k: v for k, v in hit["payload"].items() if k != "key"},
        }
        for hit in hits
    ]


def vectors_by_content_hash(
    collection: str, hashes: list[str], *, embedding_version: int
) -> dict[str, np.ndarray]:
    """按 ``content_hash`` 找**已有向量**（块级 embedding 缓存）。

    只认同一 ``embedding_version`` 的向量：换模型后旧向量不能复用（维度/语义都变了）。
    """
    if not hashes:
        return {}
    rows = qdrant_store.scroll_filter(
        collection,
        {"content_hash": list(hashes), "embedding_version": int(embedding_version)},
        with_vectors=True,
    )
    out: dict[str, np.ndarray] = {}
    for record in rows:
        payload = dict(getattr(record, "payload", None) or {})
        digest = str(payload.get("content_hash") or "")
        if not digest or digest in out:
            continue
        vector = getattr(record, "vector", None)
        if vector is None:
            continue
        out[digest] = np.asarray(vector, dtype=np.float32).reshape(-1)
    return out


def parent_keys_by_content_hash(
    doc_id: int, version: int, hashes: list[str]
) -> dict[str, str]:
    """按 ``content_hash`` 找**已写入的父块 key**（父块按内容去重时用）。

    只查同一 ``documents_id + version``：不同版本/不同文档的父块 key 不通用。
    """
    if not hashes:
        return {}
    rows = qdrant_store.scroll_filter(
        COLLECTION_PARENTS,
        {
            "documents_id": int(doc_id),
            "version": int(version),
            "content_hash": list(hashes),
        },
        with_vectors=False,
    )
    out: dict[str, str] = {}
    for record in rows:
        payload = dict(getattr(record, "payload", None) or {})
        digest = str(payload.get("content_hash") or "")
        key = str(payload.get("key") or "")
        if digest and key:
            out.setdefault(digest, key)
    return out


def count(collection: str) -> int:
    """集合里的点数（**精确**，Qdrant）。"""
    return qdrant_store.count(collection)


def stats() -> list[dict[str, Any]]:
    """各集合的占用（界面「集合与占用」数据源）。

    返回：``[{"collection", "count", "dims", "bytes", "backend"}]``。

    **目录占用只算一次**：`folder_bytes` 要 rglob 整个 Qdrant 目录，三个集合各算一遍
    就是三遍全盘遍历。这里算一次，再分摊给每个集合（各集合的 bytes 是同一个目录占用，
    界面只展示总量与集合条数，不会误解）。
    """
    names = [
        spec.name
        for spec in ALL_COLLECTIONS
        if qdrant_store.has_collection(spec.name)
    ]
    if not names:
        return []
    total_bytes = qdrant_store.folder_bytes(qdrant_store.qdrant_dir())
    out: list[dict[str, Any]] = []
    for name in names:
        info = qdrant_store.info(name, with_bytes=False)
        out.append(
            {
                "collection": name,
                "count": info["count"],
                "dims": info["dims"],
                "bytes": total_bytes,
                "backend": "qdrant",
            }
        )
    return out


def drop(collection: str) -> dict[str, Any]:
    """清空一个集合（Qdrant）。**破坏性操作**，界面要二次确认。"""
    collection = collection
    dropped = qdrant_store.delete_collection(collection)
    logger.warning("已清空集合 {}（Qdrant {}）", collection, dropped)
    return {"collection": collection, "qdrant_dropped": dropped}


def keys(collection: str) -> list[str]:
    """集合里现有 key（按需 scroll；排障/对账用，不常调）。"""
    out: list[str] = []
    for page in qdrant_store.scroll_all(collection, with_vectors=False):
        for record in page:
            payload = dict(getattr(record, "payload", None) or {})
            out.append(str(payload.get("key") or record.id))
    return out


def ensure_collections() -> None:
    """启动自检：保证 rag 的两个集合存在。"""
    ensure(COLLECTION_CHUNKS)
    ensure_parents()
