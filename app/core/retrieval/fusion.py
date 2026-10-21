"""多路结果的融合：**按名次**（RRF），不比较原始分。

为什么不用原始分相加：各功能的分数语义不同（语义相似度 / 名称命中 / 时间加权），
直接相加是错的。RRF 只看名次：``score = Σ weight_s / (k + rank_s)``。
"""

from __future__ import annotations

from dataclasses import replace

from app.core.retrieval.base import Hit


def rrf_fuse(
    ranked: dict[str, list[Hit]],
    *,
    k: int = 60,
    weights: dict[str, float] | None = None,
) -> list[Hit]:
    """Reciprocal Rank Fusion。

    参数：
        ranked: ``source -> 已排序的候选列表``。
        k: 平滑常数（常用 60；越大越平均）。
        weights: 每路的权重（缺省 1.0）。
    返回：融合后按分降序的 :class:`Hit`（``score`` 换成融合分，其余字段取自首次出现的命中）。
    """
    factors = weights or {}
    scores: dict[str, float] = {}
    best: dict[str, Hit] = {}
    for source, hits in ranked.items():
        weight = float(factors.get(source, 1.0))
        for rank, hit in enumerate(hits, start=1):
            scores[hit.key] = scores.get(hit.key, 0.0) + weight / (k + rank)
            best.setdefault(hit.key, hit)

    ordered = sorted(best.values(), key=lambda hit: scores[hit.key], reverse=True)
    return [replace(hit, score=scores[hit.key]) for hit in ordered]
