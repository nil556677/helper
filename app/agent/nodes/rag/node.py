"""RAG 子图入口：唯一检索内核（一个内核，多入口按 profile 调用）。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 并行多路
from dataclasses import dataclass, field  # 结果容器
from typing import Any  # 任意值

from app.agent.nodes.rag.graph import RagSteps, build_rag_graph  # 子图
from app.agent.tools.cache import TtlCache  # 共享缓存（检索幂等）

#: 同 (question, top_k, rerank, profile) 10 分钟内结果一致（避免每轮重跑 embed/召回/精排）。
_RETRIEVAL_CACHE = TtlCache(ttl=600.0, max_entries=2048)  # 检索缓存

#: full 档最多并行几路改写（成本上限）。
_MAX_VARIANTS = 3  # 上限


@dataclass(frozen=True)
class RagResult:  # 子图结果
    """父图只关心 evidence；计数给"检索依据"。"""

    evidence: list[dict]  # 最终证据
    hits: list[dict]  # 融合后的命中
    errors: list[dict]  # 非致命错误
    dense_count: int = 0  # 语义命中数
    sparse_count: int = 0  # 关键词命中数
    fused_count: int = 0  # 融合数
    selected_count: int = 0  # 证据数
    gate: dict[str, Any] = field(default_factory=dict)  # 门限判决（pass/weak/reject）
    profile: str = "tool"  # 实际使用的档位（观测）


class RagRunner:  # 子图运行器
    """编译一次，多次运行；一内核按 profile 装配。"""

    def __init__(self, steps: RagSteps) -> None:  # 构造
        self._graph = build_rag_graph(steps)  # 编译子图

    async def run(  # 跑一次
        self,  # 实例
        question: str,  # 问题
        *,
        profile: str = "tool",  # 检索档位：tool(轻)/full(全)/deep(深)
        top_k: int = 6,  # 证据数
        rerank: bool = True,  # 质量优先默认精排
        plan: Any | None = None,  # 检索预算（缺省按档位/条数反推）
    ) -> RagResult:  # 结果
        """不挂 checkpointer；中间态不落库。按 (问,档位,条数) 10 分钟内幂等。

        内核唯一的装配点：入口只传 profile，不各自编排检索。
        """
        from app.agent.routing.retrieval_budget import plan_for, plan_for_top_k  # 预算

        budget = plan  # 预算
        if budget is None:  # 未显式给
            budget = plan_for(question) if profile == "full" else plan_for_top_k(int(top_k))  # 按档位反推
        key = (question, int(top_k), bool(rerank), budget.level, str(profile))  # 缓存键（含档位）
        cached = _RETRIEVAL_CACHE.get(key)  # 命中检查
        if cached is not None:  # 窗口内命中
            return cached  # 直接返回（幂等）
        result = await self._invoke(question, int(top_k), bool(rerank), budget, str(profile))  # 基线（工具档）
        if str(profile) == "full":  # full 档：低置信才增强
            result = await self._enhance(question, result, int(top_k), bool(rerank), budget)  # 条件多路
        _RETRIEVAL_CACHE.set(key, result)  # 只缓存成功结果
        return result  # 返回

    async def _invoke(self, question: str, top_k: int, rerank: bool, budget: Any, profile: str) -> RagResult:  # 跑一次基线图
        """跑 RAG 子图（dense/sparse/fuse/rerank/gate/evidence），映射成 RagResult。"""
        state: dict[str, Any] = {  # 初始状态
            "question": question,  # 问题
            "top_k": int(top_k),  # 证据数
            "rerank": bool(rerank),  # 精排开关
            "plan": budget,  # 检索预算
            "profile": str(profile),  # 档位
        }  # 初始状态结束
        raw = await self._graph.ainvoke(state)  # 跑子图
        evidence = list(raw.get("evidence", []))  # 证据
        return RagResult(  # 映射结果
            evidence=evidence,  # 证据
            hits=list(raw.get("fused", [])),  # 命中
            errors=list(raw.get("errors", [])),  # 错误
            dense_count=len(raw.get("dense_hits", [])),  # 语义数
            sparse_count=len(raw.get("sparse_hits", [])),  # 关键词数
            fused_count=len(raw.get("fused", [])),  # 融合数
            selected_count=len(evidence),  # 证据数
            gate=dict(raw.get("gate") or {}),  # 门限判决
            profile=str(profile),  # 档位
        )  # 映射结束

    async def _enhance(self, question: str, base: RagResult, top_k: int, rerank: bool, budget: Any) -> RagResult:  # full 档增强
        """低置信 → Multi-Query 并行 + 可选 HyDE → 融合 → 统一精排；失败退回基线。"""
        from app.rag.confidence import should_enhance  # 置信度

        if not should_enhance(base.gate):  # 已够可信
            return base  # 不加路
        variants = await self._variants(question)  # 改写变体（LLM，失败空）
        hyde_hits = await self._hyde_hits(question, budget)  # HyDE（可选，失败空）
        if not variants and not hyde_hits:  # 无增强可用
            return base  # 基线
        extra: list[RagResult] = []  # 各变体结果
        if variants:  # 有变体
            results = await asyncio.gather(  # 并行检索
                *(self._invoke(v, top_k, rerank, budget, "full") for v in variants),  # 每路
                return_exceptions=True,  # 单路失败不拖垮
            )  # 结束
            extra = [r for r in results if isinstance(r, RagResult)]  # 只留成功
        candidates = self._collect(base, extra, hyde_hits)  # 汇总候选
        if not candidates:  # 无候选
            return base  # 基线
        try:  # 统一精排（融合只做一次）
            from app.rag import retrieval  # 检索原语

            ranked = await asyncio.to_thread(retrieval.rerank, question, candidates, int(getattr(budget, "rerank_top", 10)))  # 精排
            evidence = await asyncio.to_thread(retrieval.evidence, ranked, budget)  # 证据（断崖/预算）
        except Exception:  # 精排不可用
            return base  # 退回基线
        if not evidence:  # 精排后没证据
            return base  # 基线
        dense = base.dense_count + sum(r.dense_count for r in extra)  # 合计
        sparse = base.sparse_count + sum(r.sparse_count for r in extra)  # 合计
        return RagResult(  # 增强结果
            evidence=evidence,  # 证据
            hits=candidates,  # 候选（融合池）
            errors=base.errors,  # 错误
            dense_count=dense,  # 语义数
            sparse_count=sparse,  # 关键词数
            fused_count=len(candidates),  # 融合数
            selected_count=len(evidence),  # 证据数
            gate=base.gate,  # 门限沿用基线
            profile="full",  # 档位
        )  # 结束

    async def _variants(self, question: str) -> list[str]:  # 改写变体（多路）
        """调改写拿 2..N 条不同视角的查询；任何失败返回空。"""
        try:  # 模型/网关不可用
            from app.rag import rewrite  # 改写

            planned = await rewrite.rewrite(question)  # 写
            return list(planned.queries[1 : _MAX_VARIANTS + 1])  # 去掉原句锚
        except Exception:  # 失败
            return []  # 空

    async def _hyde_hits(self, question: str, budget: Any) -> list[dict]:  # HyDE 稠密召回
        """flag 开启且有模型 → 假设答案向量召回；失败返回空。"""
        try:  # 任何失败退回
            from app.rag import hyde, retrieval  # 模块

            if not hyde.enabled():  # 未开启
                return []  # 空
            text = await hyde.hypothetical(question)  # 假设答案
            if not text:  # 空
                return []  # 空
            vector = retrieval.embed_query(text)  # 假设答案向量
            return list(retrieval.dense_search(vector, k=int(getattr(budget, "dense", 40))))  # 语义召回
        except Exception:  # 失败
            return []  # 空

    def _collect(self, base: RagResult, extra: list[RagResult], hyde_hits: list[dict]) -> list[dict]:  # 汇总候选
        """把各路证据/命中归一成 rerank 候选（按 text 去重）。"""
        seen: set[str] = set()  # 去重
        out: list[dict] = []  # 候选

        def _add(text: str, meta: dict[str, Any]) -> None:  # 添加一条
            body = str(text or "").strip()  # 文本
            if not body or body in seen:  # 空/重复
                return  # 跳过
            seen.add(body)  # 记
            out.append({"text": body, "metadata": dict(meta or {})})  # 候选

        for result in [base, *extra]:  # 核心 + 变体
            for item in result.evidence:  # 证据
                meta = {"source_name": item.get("source_name", ""), "origin": item.get("origin", ""), "score": item.get("score")}  # 元数据
                _add(str(item.get("text") or ""), meta)  # 加
        for hit in hyde_hits:  # HyDE 命中
            _add(str(hit.get("text") or ""), dict(hit.get("metadata") or {}))  # 加
        return out  # 返回