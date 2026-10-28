"""RRF 权重与候选归并的小回归。"""

from __future__ import annotations

from langchain_core.documents import Document

from app.integrations.retrievers import rrf_fuse


def _doc(key: str) -> Document:
    return Document(page_content=key, metadata={"chunk_key": key})


def test_rrf_dense_weight_can_boost_dense_only_hit():
    fused = rrf_fuse(
        [_doc("dense")],
        [_doc("sparse")],
        k=60,
        top_k=10,
        dense_weight=2.0,
        sparse_weight=1.0,
    )
    by_key = {doc.metadata["chunk_key"]: score for doc, score in fused}
    assert by_key["dense"] > by_key["sparse"]


def test_rrf_weights_zero_drops_channel():
    fused = rrf_fuse(
        [_doc("dense")],
        [_doc("sparse")],
        k=60,
        top_k=10,
        dense_weight=0.0,
        sparse_weight=1.0,
    )
    keys = [doc.metadata["chunk_key"] for doc, _ in fused]
    assert "dense" not in keys and "sparse" in keys
