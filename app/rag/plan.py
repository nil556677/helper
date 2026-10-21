"""RAG 的召回参数（**每功能一份**，不是全局一份）。

数值与文档/记忆/工具的差异都在这里显式声明；编排层读它来组合。
"""

from app.core.retrieval.base import RetrievalPlan
from app.rag.retrieval import RagRetriever

#: 文档问答：语义召回候选较多（靠量保召回），融合权重 1.0。
RAG_PLAN = RetrievalPlan(
    retriever=RagRetriever(),
    candidate_k=50,
    mode="dense",
    weight=1.0,
)
