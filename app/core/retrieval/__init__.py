"""检索引擎：共享契约 + 词法分词 + 多路融合。

- 契约：:mod:`app.core.retrieval.base`（``Hit`` / ``Retriever`` / ``RetrievalPlan``）；
- 融合：:mod:`app.core.retrieval.fusion`（RRF，按名次）；
- 编排：:mod:`app.core.retrieval.pipeline`（``recall``）；
- 分词：:mod:`app.core.retrieval.tokenize`（写入与查询**必须共用同一套切法**）。

具体功能的召回实现放在各自 ``app/core/features/<feature>/retrieval.py``，
这里**不认识任何具体 collection**。
"""

from app.core.retrieval.base import Hit, RetrievalPlan, Retriever
from app.core.retrieval.fusion import rrf_fuse
from app.core.retrieval.pipeline import recall
from app.core.retrieval.tokenize import cut, to_tokens

__all__ = [
    "Hit",
    "RetrievalPlan",
    "Retriever",
    "cut",
    "recall",
    "rrf_fuse",
    "to_tokens",
]
