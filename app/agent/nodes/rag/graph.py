"""RAG 子图：只连边，七个步骤由 RagSteps 注入。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 阻塞步骤丢线程
from collections.abc import Callable  # 步骤签名
from dataclasses import dataclass  # 步骤容器
from typing import Any  # 任意返回值

from langgraph.graph import END, START, StateGraph  # 图原语

from app.agent.contracts import StageGroup  # 步骤分组
from app.agent.nodes.rag.state import RagState  # 子图状态
from app.agent.stages import stage  # 步骤事件


@dataclass(frozen=True)
class RagSteps:  # 七个纯步骤
    """真实实现放 app/rag/retrieval.py，这里只注入。"""

    embed: Callable[[str], list[float]]  # 问题 -> 向量
    dense: Callable[[list[float], int], list[dict]]  # 向量 -> 语义 hits
    sparse: Callable[[str, int], list[dict]]  # 问题 -> 关键词 hits
    fuse: Callable[[list[dict], list[dict]], list[dict]]  # 两路 -> 融合
    backfill: Callable[[list[dict]], list[dict]]  # 融合 -> 候选
    rerank: Callable[[str, list[dict]], list[dict]]  # 问题+候选 -> 精排
    gate: Callable[[str, list[dict]], Any]  # 问题+精排 -> 门限判决（带 kept）
    evidence: Callable[[list[dict], int], list[dict]]  # 精排 -> 证据


def _plan_of(state: RagState) -> Any:  # 取检索预算
    """优先用 state 里的 RetrievalPlan；没有返回 None（走默认常量）。"""
    return state.get("plan") if isinstance(state.get("plan"), object) else None


def build_rag_graph(steps: RagSteps):  # 建 RAG 子图
    """节点只读 state、调 step、写 state；不写库。"""
    graph = StateGraph(RagState)  # 状态图

    async def embed_node(state: RagState) -> dict[str, Any]:  # 查询向量
        """问题 -> 向量。"""
        with stage("理解问题", group=StageGroup.PREPARE) as info:  # 步骤事件
            vector = await asyncio.to_thread(steps.embed, state["question"])  # 嵌入
            info.detail = "提取问题关键词"  # 明细
        return {"query_vector": vector}  # 写回

    async def dense_node(state: RagState) -> dict[str, Any]:  # 语义召回
        """向量 -> dense hits（候选数随预算）。"""
        plan = _plan_of(state)  # 预算
        k = int(plan.dense) if plan is not None else 40  # 候选数
        with stage("查找资料", group=StageGroup.SEARCH) as info:  # 步骤事件
            hits = await asyncio.to_thread(steps.dense, state["query_vector"], k)  # 查询
            info.detail = f"找到 {len(hits)} 段资料"  # 明细
        return {"dense_hits": hits}  # 写回

    async def sparse_node(state: RagState) -> dict[str, Any]:  # 关键词召回
        """问题 -> sparse hits（候选数随预算）。"""
        plan = _plan_of(state)  # 预算
        k = int(plan.sparse) if plan is not None else 50  # 候选数
        with stage("查找资料", group=StageGroup.SEARCH) as info:  # 步骤事件
            hits = await asyncio.to_thread(steps.sparse, state["question"], k)  # 查询
            info.detail = f"找到 {len(hits)} 段资料"  # 明细
        return {"sparse_hits": hits}  # 写回

    async def fuse_node(state: RagState) -> dict[str, Any]:  # RRF 融合
        """两路 -> 融合（权重随预算）。"""
        plan = _plan_of(state)  # 预算
        with stage("合并结果", group=StageGroup.SEARCH) as info:  # 步骤事件
            if plan is not None:  # 有预算
                fused = await asyncio.to_thread(  # 融合
                    steps.fuse,  # 步骤
                    state.get("dense_hits", []),  # 语义
                    state.get("sparse_hits", []),  # 关键词
                    weights=(float(plan.dense_weight), float(plan.sparse_weight)),  # 权重
                )  # 结束
            else:  # 默认
                fused = await asyncio.to_thread(  # 融合
                    steps.fuse, state.get("dense_hits", []), state.get("sparse_hits", [])  # 默认
                )  # 结束
            info.detail = f"合并 {len(fused)} 段"  # 明细
        return {"fused": fused}  # 写回

    async def backfill_node(state: RagState) -> dict[str, Any]:  # 父块回填
        """融合 -> 候选。"""
        with stage("补全上下文", group=StageGroup.SEARCH) as info:  # 步骤事件
            candidates = await asyncio.to_thread(steps.backfill, state.get("fused", []))  # 回填
            info.detail = f"补全 {len(candidates)} 段"  # 明细
        return {"candidates": candidates}  # 写回

    async def rerank_node(state: RagState) -> dict[str, Any]:  # 精排
        """问题 + 候选 -> 精排。"""
        with stage("筛选重点", group=StageGroup.FILTER) as info:  # 步骤事件
            ranked = await asyncio.to_thread(  # 精排
                steps.rerank, state["question"], state.get("candidates", [])  # 参数
            )  # 精排结束
            info.detail = f"选出 {len(ranked)} 段"  # 明细
        return {"ranked": ranked}  # 写回

    async def gate_node(state: RagState) -> dict[str, Any]:  # 证据门限
        """精排结果 -> 三态判决，并把不合格的丢掉。"""
        with stage("判断证据", group=StageGroup.FILTER) as info:  # 步骤事件
            decision = await asyncio.to_thread(  # 门限
                steps.gate, state["question"], state.get("ranked", [])  # 参数
            )  # 判决结束
            info.detail = f"{decision.decision} · {len(decision.kept)} 条"  # 明细
        return {"ranked": list(decision.kept), "gate": decision.as_dict()}  # 写回

    async def evidence_node(state: RagState) -> dict[str, Any]:  # 证据
        """精排 -> 证据。"""
        with stage("整理引用", group=StageGroup.FILTER) as info:  # 步骤事件
            items = await asyncio.to_thread(  # 组装
                steps.evidence, state.get("ranked", []), int(state.get("top_k") or 6)  # 参数
            )  # 组装结束
            info.detail = f"{len(items)} 段引用"  # 明细
        return {"evidence": items}  # 写回

    graph.add_node("embed_query", embed_node)  # 查询向量
    graph.add_node("dense_recall", dense_node)  # 语义召回
    graph.add_node("sparse_recall", sparse_node)  # 关键词召回
    graph.add_node("fuse", fuse_node)  # 融合
    graph.add_node("backfill", backfill_node)  # 父块回填
    graph.add_node("rerank", rerank_node)  # 精排
    graph.add_node("gate", gate_node)  # 门限
    graph.add_node("evidence", evidence_node)  # 证据

    graph.add_edge(START, "embed_query")  # 入口到向量
    graph.add_edge(START, "sparse_recall")  # 入口到关键词（并行）
    graph.add_edge("embed_query", "dense_recall")  # 向量后语义
    graph.add_edge(["dense_recall", "sparse_recall"], "fuse")  # join：两路都完成
    graph.add_edge("fuse", "backfill")  # 融合到回填
    graph.add_edge("backfill", "rerank")  # 回填到精排
    graph.add_edge("rerank", "gate")  # 精排到门限
    graph.add_edge("gate", "evidence")  # 门限到证据
    graph.add_edge("evidence", END)  # 证据到结束

    return graph.compile()  # 编译
