"""RAG 子图：七个步骤、并行召回、join、证据输出。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 事件循环

from app.agent.nodes.rag import RagRunner, RagSteps  # 子图入口


def _steps(calls: list[str]) -> RagSteps:  # 造假步骤
    """记录调用顺序，返回固定结果。"""
    def embed(question: str) -> list[float]:  # 查询向量
        calls.append("embed")  # 记录
        return [1.0, 0.0]  # 假向量

    def dense(vector: list[float], k: int) -> list[dict]:  # 语义召回
        calls.append("dense")  # 记录
        return [{"chunk_key": "a", "score": 0.9}]  # 一条

    def sparse(question: str, k: int) -> list[dict]:  # 关键词召回
        calls.append("sparse")  # 记录
        return [{"chunk_key": "b", "score": 0.8}]  # 一条

    def fuse(dense_hits: list[dict], sparse_hits: list[dict]) -> list[dict]:  # 融合
        calls.append("fuse")  # 记录
        return [*dense_hits, *sparse_hits]  # 合并

    def backfill(fused: list[dict]) -> list[dict]:  # 回填
        calls.append("backfill")  # 记录
        return [{"chunk_key": item["chunk_key"], "text": item["chunk_key"]} for item in fused]  # 候选

    def rerank(question: str, candidates: list[dict]) -> list[dict]:  # 精排
        calls.append("rerank")  # 记录
        return candidates  # 原样

    class _Decision:  # 假判决
        """只保留前两条，判 PASS。"""

        decision = "pass"  # 判决
        kept = None  # 延迟

        def as_dict(self) -> dict:  # 转字典
            """轨迹用。"""
            return {"decision": self.decision, "count": len(self.kept or [])}  # 载荷

    def gate(question: str, ranked: list[dict]) -> _Decision:  # 门限
        calls.append("gate")  # 记录
        decision = _Decision()  # 实例
        decision.kept = ranked[:2]  # 留两条
        return decision  # 返回

    def evidence(ranked: list[dict], top_k: int) -> list[dict]:  # 证据
        calls.append("evidence")  # 记录
        return ranked[:top_k]  # 截断

    return RagSteps(  # 返回步骤
        embed=embed,  # 向量
        dense=dense,  # 语义
        sparse=sparse,  # 关键词
        fuse=fuse,  # 融合
        backfill=backfill,  # 回填
        rerank=rerank,  # 精排
        gate=gate,  # 门限
        evidence=evidence,  # 证据
    )  # 步骤结束


def test_rag_subgraph_runs_all_steps() -> None:  # 全链路
    """八个步骤都执行，输出 2 条证据。"""
    calls: list[str] = []  # 记录
    runner = RagRunner(_steps(calls))  # 运行器
    result = asyncio.run(runner.run("问题", top_k=6, rerank=True))  # 跑子图
    assert len(result.evidence) == 2  # 两条证据
    assert result.hits  # 有融合结果
    assert result.gate.get("decision") == "pass"  # 门限结论回传
    assert set(calls) == {"embed", "dense", "sparse", "fuse", "backfill", "rerank", "gate", "evidence"}  # 全执行


def test_rag_subgraph_respects_top_k() -> None:  # top_k
    """证据按 top_k 截断。"""
    calls: list[str] = []  # 记录
    runner = RagRunner(_steps(calls))  # 运行器
    result = asyncio.run(runner.run("问题", top_k=1, rerank=True))  # 只取一条
    assert len(result.evidence) == 1  # 一条
