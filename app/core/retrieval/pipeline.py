"""多路召回编排：``plans`` → 各路召回 → 融合 → 截断。

**top-K 分两层**：每路先按自己的 ``candidate_k`` 取候选，融合后最终只留编排层给的
``final_k``。功能**不感知** ``final_k``。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.retrieval.base import Hit, RetrievalPlan
from app.core.retrieval.fusion import rrf_fuse


def recall(plans: Sequence[RetrievalPlan], query: str, *, final_k: int) -> list[Hit]:
    """多路召回 + 融合 + 截断。

    参数：
        plans: 本次要用的各路计划（``enabled=False`` 的会被跳过）。
        query: 用户查询原文。
        final_k: 融合后最终保留几条（由编排层按上下文预算决定）。
    返回：融合后的 :class:`Hit` 列表，最多 ``final_k`` 条。
    """
    ranked: dict[str, list[Hit]] = {}
    weights: dict[str, float] = {}
    for plan in plans:
        if not plan.enabled or plan.candidate_k <= 0:
            continue
        hits = plan.retriever.search(query, limit=plan.candidate_k)
        if plan.min_score is not None:
            hits = [hit for hit in hits if hit.score >= plan.min_score]
        ranked[plan.source] = hits
        weights[plan.source] = plan.weight

    if not ranked:
        return []
    fused = rrf_fuse(ranked, weights=weights)
    return fused[: max(0, final_k)]
