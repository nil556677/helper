"""RAG 子图入口：把父 state 映射成 RagState，再把 evidence 写回。"""

from __future__ import annotations  # 类型注解延迟求值

from dataclasses import dataclass, field  # 结果容器
from typing import Any  # 任意值

from app.agent.nodes.rag.graph import RagSteps, build_rag_graph  # 子图
from app.agent.tools.cache import TtlCache  # 共享缓存（检索幂等）

#: 同 (question, top_k, rerank) 10 分钟内结果一致（避免每轮重跑 embed/召回/精排）。
_RETRIEVAL_CACHE = TtlCache(ttl=600.0, max_entries=2048)  # 检索缓存


@dataclass(frozen=True)
class RagResult:  # 子图结果
    """父图只关心 evidence；计数给“检索依据”。"""

    evidence: list[dict]  # 最终证据
    hits: list[dict]  # 融合后的命中
    errors: list[dict]  # 非致命错误
    dense_count: int = 0  # 语义命中数
    sparse_count: int = 0  # 关键词命中数
    fused_count: int = 0  # 融合数
    selected_count: int = 0  # 证据数
    gate: dict[str, Any] = field(default_factory=dict)  # 门限判决（pass/weak/reject）


class RagRunner:  # 子图运行器
    """编译一次，多次运行。"""

    def __init__(self, steps: RagSteps) -> None:  # 构造
        self._graph = build_rag_graph(steps)  # 编译子图

    async def run(  # 跑一次
        self,  # 实例
        question: str,  # 问题
        *,
        top_k: int = 6,  # 证据数
        rerank: bool = True,  # 质量优先默认精排
        plan: Any | None = None,  # 检索预算（缺省按 top_k 反推）
    ) -> RagResult:  # 结果
        """不挂 checkpointer；中间态不落库。检索结果按问 10 分钟内幂等。"""
        from app.agent.routing.retrieval_budget import plan_for_top_k  # 反推

        budget = plan if plan is not None else plan_for_top_k(int(top_k))  # 预算
        key = (question, int(top_k), bool(rerank), budget.level)  # 缓存键（含档位）
        cached = _RETRIEVAL_CACHE.get(key)  # 命中检查
        if cached is not None:  # 窗口内命中
            return cached  # 直接返回（幂等）
        state: dict[str, Any] = {  # 初始状态
            "question": question,  # 问题
            "top_k": int(top_k),  # 证据数
            "rerank": bool(rerank),  # 精排开关
            "plan": budget,  # 检索预算（驱动各阶段参数与断崖切分）
        }  # 初始状态结束
        result = await self._graph.ainvoke(state)  # 跑子图
        evidence = list(result.get("evidence", []))  # 证据
        wrapped = RagResult(  # 映射结果
            evidence=evidence,  # 证据
            hits=list(result.get("fused", [])),  # 命中
            errors=list(result.get("errors", [])),  # 错误
            dense_count=len(result.get("dense_hits", [])),  # 语义数
            sparse_count=len(result.get("sparse_hits", [])),  # 关键词数
            fused_count=len(result.get("fused", [])),  # 融合数
            selected_count=len(evidence),  # 证据数
            gate=dict(result.get("gate") or {}),  # 门限判决
        )  # 映射结束
        _RETRIEVAL_CACHE.set(key, wrapped)  # 只缓存成功结果
        return wrapped  # 返回
