"""RAG 召回器：查询向量化 → 在 ``chunks`` 集合里找 ``active`` 片段。

实现 :class:`app.core.retrieval.base.Retriever` 契约。**只召回 child**（``chunks``）：
parent 是送给模型的证据，命中 child 后按 ``parent_key`` 回填由编排层决定——
召回单位 = 检索单位，不在这一层展开。

检索参数（``candidate_k`` / 权重 / 下限）由 :mod:`app.rag.plan` 声明，
本类只负责"给定 query 和条数，返回到排序好的 Hit"。
"""

from __future__ import annotations

from app.core.embed import embed_text
from app.core.retrieval.base import Hit
from app.core.vector import api


class RagRetriever:
    """文档问答的召回器（dense 语义检索）。"""

    source = "rag"

    def search(self, query: str, *, limit: int) -> list[Hit]:
        """按语义相似度召回 ``limit`` 条 active 片段。"""
        if not query.strip() or limit <= 0:
            return []
        vector = embed_text(query)
        rows = api.search(
            api.COLLECTION_CHUNKS,
            vector,
            top_k=int(limit),
            where={"status": "active"},
        )
        return [
            Hit(
                key=str(row["key"]),
                source=self.source,
                text=str(row["payload"].get("text") or ""),
                score=float(row["score"]),
                payload=dict(row["payload"]),
            )
            for row in rows
        ]
