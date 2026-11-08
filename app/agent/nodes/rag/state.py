"""RAG 子图状态：只放可序列化的中间结果。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any, TypedDict  # 状态类型


class RagState(TypedDict, total=False):  # RAG 子图状态
    """父图不直接用；由 nodes/rag/node.py 映射。"""

    question: str  # 用户问题
    top_k: int  # 最终证据数
    rerank: bool  # 是否精排（质量优先默认 True）
    query_vector: list[float]  # 查询向量（骨架阶段暂时放 state）
    dense_hits: list[dict]  # 语义召回
    sparse_hits: list[dict]  # 关键词召回
    fused: list[dict]  # RRF 融合
    candidates: list[dict]  # 父块/整行回填
    ranked: list[dict]  # 精排后
    gate: dict  # 门限判决（pass/weak/reject + 分数）
    evidence: list[dict]  # 最终证据
    errors: list[dict]  # 非致命错误
    plan: Any  # 检索预算（RetrievalPlan；缺省按 top_k 反推）
    profile: str  # 检索档位（tool/full/deep；决定装配哪些路）
