"""向量层：**Qdrant 是唯一向量后端与检索来源**（docs/向量层重构设计.md）。

对外只用 :mod:`app.core.vector.api` 里的函数：

```python
api.ensure(COLLECTION_CHUNKS)                                    # 建集合 + payload 索引（幂等）
api.ensure_parents()                                             # 建 parent 的无向量集合
api.upsert(COLLECTION_CHUNKS, [{"key": "12:1:0", "vector": v, "payload": {...}}])
api.search(COLLECTION_CHUNKS, query_vector, top_k=50, where={"status": "active"})
api.count(COLLECTION_CHUNKS) / api.stats()                       # 对账与界面占用
```

模块分工：

- :mod:`~app.core.vector.api` —— 对外编排（唯一入口）；
- :mod:`~app.core.vector.qdrant_store` —— Qdrant 读写（服务端/本地，事实来源）。

**为什么要有这一层**（而不是各处直接调 Qdrant）：向量是一个**通用设施**——知识库问答、
记忆召回、给大模型补知识都要"找相似的"，它们不该各自实现一遍"怎么存、怎么算分、怎么过滤"。
"""

from app.core.vector.api import (
    COLLECTION_CHUNKS,
    COLLECTION_MEMORY,
    COLLECTION_PARENTS,
    VectorError,
    VectorSession,
    backend,
    count,
    dims,
    drop,
    ensure,
    ensure_parents,
    keys,
    search,
    stats,
    upsert,
)
from app.core.vector.qdrant_store import (
    VectorStoreError,
    build_payload,
    close,
    point_id,
    qdrant_dir,
    stable_point_id,
)

__all__ = [
    "COLLECTION_CHUNKS",
    "COLLECTION_MEMORY",
    "COLLECTION_PARENTS",
    "VectorError",
    "VectorSession",
    "VectorStoreError",
    "backend",
    "build_payload",
    "close",
    "count",
    "dims",
    "drop",
    "ensure",
    "ensure_parents",
    "keys",
    "point_id",
    "qdrant_dir",
    "search",
    "stable_point_id",
    "stats",
    "upsert",
]
