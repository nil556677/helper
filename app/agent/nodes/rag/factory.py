"""RAG 真实步骤工厂：把 app/rag/retrieval.py 注入子图。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.nodes.rag.graph import RagSteps  # 步骤契约
from app.rag import gate as gate_mod  # 门限
from app.rag import retrieval  # 纯函数


def real_steps() -> RagSteps:  # 真实步骤
    """质量优先：精排保持开启。"""
    return RagSteps(  # 组装
        embed=retrieval.embed_query,  # 查询向量
        dense=retrieval.dense_search,  # 语义召回
        sparse=retrieval.sparse_search,  # 关键词召回
        fuse=retrieval.fuse_hits,  # RRF
        backfill=retrieval.backfill,  # 父块回填
        rerank=retrieval.rerank,  # 精排
        gate=gate_mod.gate_hits,  # 证据门限（三态 + QA 方向性）
        evidence=retrieval.evidence,  # 证据
    )  # 返回结束
