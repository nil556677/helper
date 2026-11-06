"""检索预算：按问题复杂度/意图动态决定各阶段参数（简单↔复杂取不同档）。"""

from __future__ import annotations  # 延迟求值注解

from dataclasses import dataclass  # 数据类
from typing import Any  # 任意值

__all__ = ["RetrievalPlan", "plan_for", "plan_for_top_k"]  # 导出


@dataclass(frozen=True)
class RetrievalPlan:  # 一次检索的参数组
    """各阶段候选数/重排数/最终证据数 + 断崖切分参数 + RRF 权重。"""

    level: str = "simple"  # simple / medium / complex
    dense: int = 40  # 语义候选
    sparse: int = 50  # 关键词候选
    fused: int = 50  # 融合后保留
    rerank_top: int = 10  # 精排保留
    final_max: int = 6  # 证据上限
    final_min: int = 2  # 证据下限
    min_results: int = 3  # 质量判定"最少有效命中"
    cliff_ratio: float = 2.0  # 断崖相对占比
    min_cliff: float = 0.05  # 断崖绝对分差
    dense_weight: float = 1.0  # RRF 语义权重
    sparse_weight: float = 1.0  # RRF 关键词权重


#: 三档默认。
SIMPLE = RetrievalPlan(level="simple", final_max=4, final_min=1, min_results=2)  # 简单
MEDIUM = RetrievalPlan(level="medium", dense=60, sparse=80, fused=80, rerank_top=16, final_max=6, final_min=2, min_results=3)  # 中等
COMPLEX = RetrievalPlan(  # 复杂
    level="complex",  # 档
    dense=100,  # 语义
    sparse=120,  # 关键词
    fused=110,  # 融合
    rerank_top=24,  # 精排
    final_max=9,  # 证据
    final_min=2,  # 下限
    min_results=4,  # 质量
    sparse_weight=1.3,  # 复杂多关键词 → 抬 sparse
)  # 结束

#: 多跳/对比/复杂意图信号。
_COMPLEX_MARKERS: tuple[str, ...] = (  # 依赖链/多跳
    "为什么", "原因", "导致", "排查", "根因", "如果", "那么", "原理", "怎么才能", "步骤", "对比", "区别", "清单", "分别", "各自", "如何实现",
)  # 标记


def plan_for(  # 按问题+意图出预算
    query: str,  # 问题
    *,
    mode: str = "",  # flags.mode（deep_research 强制复杂）
    path_type: str = "",  # triage.path_type（agentic 复杂）
    length_threshold_simple: int = 40,  # 简单问题长度阈值
) -> RetrievalPlan:  # 预算
    """信号：深研/agentic → 复杂；有复杂标记或较长 → 复杂；否则简单。"""
    text = str(query or "")  # 原文
    if mode == "deep_research" or path_type == "agentic":  # 强信号
        return COMPLEX  # 复杂
    if any(word in text for word in _COMPLEX_MARKERS):  # 有复杂标记
        return COMPLEX  # 复杂
    if len(text) >= length_threshold_simple:  # 长问题
        return COMPLEX  # 复杂
    if len(text) >= 14:  # 稍长
        return MEDIUM  # 中等
    return SIMPLE  # 简单


def plan_for_top_k(top_k: int) -> RetrievalPlan:  # 由最终证据数反推预算（工具壳用）
    """搜索工具的顶层 top_k 直接映射到档位，保证同一把尺。"""
    if int(top_k) >= 8:  # 复杂（search_deep 等大召回）
        return COMPLEX  # 复杂
    if int(top_k) >= 6:  # 中（search_hybrid 默认 10 → 复杂，但 6~7 命中中档）
        return MEDIUM  # 中等
    return SIMPLE  # 简单