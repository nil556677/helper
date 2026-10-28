"""LangChain 检索器：dense/sparse 两路召回 + RRF 融合（单集合 kb_chunks）。

- `ChannelRetriever`：一路召回，记录 trace_hit（stage=dense/sparse）；
- `HybridRetriever`：两路依次执行，Python RRF 融合，记录 stage=fused；
- 过滤用顶层 payload 的 `status == "active"`（入库时由 writer 设置）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from app.core.trace.recorder import trace_hit

ACTIVE_STATUS = "active"


def active_filter() -> Any:
    from qdrant_client import models

    return models.Filter(
        must=[
            models.FieldCondition(
                key="status", match=models.MatchValue(value=ACTIVE_STATUS)
            )
        ]
    )


class ChannelRetriever(BaseRetriever):
    """一路召回（dense 或 sparse）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    store: Any
    stage: str  # "dense" / "sparse"
    k: int = 40
    has_terms: Callable[[str], bool] | None = None

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None
    ) -> list[Document]:
        del run_manager
        if self.stage == "sparse" and self.has_terms is not None and not self.has_terms(query):
            return []
        pairs = self.store.similarity_search_with_score(query, k=self.k, filter=active_filter())
        score_kind = "cosine" if self.stage == "dense" else "bm25"
        for rank, (doc, score) in enumerate(pairs, 1):
            meta = doc.metadata or {}
            trace_hit(
                stage=self.stage,
                rank=rank,
                chunk_id=meta.get("chunk_id"),
                chunk_key=meta.get("chunk_key"),
                doc_id=meta.get("doc_id"),
                source_name=meta.get("source_name", ""),
                heading=meta.get("heading", ""),
                loc=meta.get("loc", {}),
                score=float(score),
                score_kind=score_kind,
                text=doc.page_content,
            )
        return [doc for doc, _ in pairs]


def _key(doc: Document) -> str:
    meta = doc.metadata or {}
    return str(meta.get("chunk_key") or meta.get("_id") or "")


def rrf_fuse(
    dense: list[Document],
    sparse: list[Document],
    *,
    k: int = 60,
    top_k: int = 50,
    dense_weight: float = 1.0,
    sparse_weight: float = 1.0,
) -> list[tuple[Document, float]]:
    """两路有序文档的 RRF 融合，返回 `(doc, score)`（score 是名次分）。"""
    scores: dict[str, float] = {}
    dw = max(0.0, float(dense_weight))
    sw = max(0.0, float(sparse_weight))
    for rank, doc in enumerate(dense, 1):
        key = _key(doc)
        if key:
            scores[key] = scores.get(key, 0.0) + dw / (k + rank)
    for rank, doc in enumerate(sparse, 1):
        key = _key(doc)
        if key:
            scores[key] = scores.get(key, 0.0) + sw / (k + rank)
    by_key: dict[str, Document] = {}
    for doc in [*dense, *sparse]:
        key = _key(doc)
        if key and key not in by_key:
            by_key[key] = doc
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
    return [
        (by_key[key], score)
        for key, score in ordered
        if key in by_key and score > 0
    ]


class HybridRetriever(BaseRetriever):
    """dense + sparse → RRF（顺序执行，保证 trace 顺序稳定）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    dense: BaseRetriever
    sparse: BaseRetriever
    top_k: int = 50
    rrf_k: int = 60
    dense_weight: float = 1.0
    sparse_weight: float = 1.0

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None
    ) -> list[Document]:
        del run_manager
        dense_docs = self.dense.invoke(query)
        sparse_docs = self.sparse.invoke(query)
        fused = rrf_fuse(
            dense_docs,
            sparse_docs,
            k=self.rrf_k,
            top_k=self.top_k,
            dense_weight=self.dense_weight,
            sparse_weight=self.sparse_weight,
        )
        for rank, (doc, score) in enumerate(fused, 1):
            meta = doc.metadata or {}
            trace_hit(
                stage="fused",
                rank=rank,
                chunk_id=meta.get("chunk_id"),
                chunk_key=meta.get("chunk_key"),
                doc_id=meta.get("doc_id"),
                source_name=meta.get("source_name", ""),
                heading=meta.get("heading", ""),
                loc=meta.get("loc", {}),
                score=float(score),
                score_kind="rrf",
                text=doc.page_content,
            )
        return [doc for doc, _ in fused]
