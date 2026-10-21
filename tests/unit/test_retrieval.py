"""多源检索契约的单测：Hit / RRF / recall（全部用假召回器，不碰 Qdrant）。"""

from __future__ import annotations

from app.core.retrieval.base import Hit, RetrievalPlan
from app.core.retrieval.fusion import rrf_fuse
from app.core.retrieval.pipeline import recall


class _FakeRetriever:
    """记录调用、返回固定候选的假召回器。"""

    def __init__(self, source: str, hits: list[Hit]) -> None:
        self.source = source
        self._hits = hits
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int) -> list[Hit]:
        self.calls.append((query, limit))
        return self._hits[:limit]


def _hit(key: str, source: str, score: float = 1.0) -> Hit:
    return Hit(key=key, source=source, text=key, score=score)


def test_rrf_ranks_by_position_not_raw_score() -> None:
    """融合看名次：a2 原始分低但排在 a1 后面，仍应高于只有一条的 b1 之外的项。"""
    ranked = {
        "a": [_hit("a1", "a", 0.9), _hit("a2", "a", 0.1)],
        "b": [_hit("b1", "b", 0.2)],
    }
    fused = rrf_fuse(ranked, k=60)
    # a1 与 b1 都是各路第 1，同分；a2 是第 2，落后
    assert [hit.key for hit in fused] == ["a1", "b1", "a2"]
    assert fused[0].score > 0


def test_rrf_weight_changes_order() -> None:
    ranked = {"a": [_hit("a1", "a")], "b": [_hit("b1", "b")]}
    fused = rrf_fuse(ranked, weights={"b": 5.0})
    assert fused[0].key == "b1"


def test_recall_respects_candidate_k_and_final_k() -> None:
    a = _FakeRetriever("a", [_hit("a1", "a"), _hit("a2", "a"), _hit("a3", "a")])
    b = _FakeRetriever("b", [_hit("b1", "b"), _hit("b2", "b")])
    plans = [
        RetrievalPlan(retriever=a, candidate_k=2),
        RetrievalPlan(retriever=b, candidate_k=1),
    ]
    hits = recall(plans, "q", final_k=2)

    assert len(hits) == 2
    assert a.calls == [("q", 2)]
    assert b.calls == [("q", 1)]


def test_recall_skips_disabled_without_calling() -> None:
    a = _FakeRetriever("a", [_hit("a1", "a")])
    plans = [RetrievalPlan(retriever=a, candidate_k=5, enabled=False)]
    assert recall(plans, "q", final_k=5) == []
    assert a.calls == []


def test_recall_min_score_filters() -> None:
    a = _FakeRetriever("a", [_hit("hi", "a", 0.9), _hit("lo", "a", 0.1)])
    plans = [RetrievalPlan(retriever=a, candidate_k=5, min_score=0.5)]
    assert [hit.key for hit in recall(plans, "q", final_k=5)] == ["hi"]


def test_recall_empty_plans_returns_empty() -> None:
    assert recall([], "q", final_k=3) == []
