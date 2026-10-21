"""多源检索的**共享契约**：``Hit`` / ``Retriever`` / ``RetrievalPlan``。

设计见 ``docs/多源检索设计.md``。这里只定"形状"，不写任何具体检索实现：

- :class:`Hit` —— 跨源统一的输出形状；
- :class:`Retriever` —— 每个功能实现一个，**只管怎么召**；
- :class:`RetrievalPlan` —— 一个功能"这次怎么召"的参数（**每功能各一份**）。

**不在这里做的事**：不决定最终取几条（那是编排层的 ``final_k``）、不比较跨源原始分
（见 :mod:`app.core.retrieval.fusion`）、不认识任何具体 collection。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Hit:
    """一条召回结果。

    参数：
        key: 实体唯一键（``mem:42`` / ``tool:search`` / ``12:1:0``）。
        source: 来源功能名（``"rag"`` / ``"memory"`` / ``"tools"``）。
        text: 可直接拼进上下文的那段话。
        score: **本路自己的分**（不跨源比较；融合时按名次，不用它）。
        payload: 原始 payload，编排层按需取字段。
    """

    key: str
    source: str
    text: str
    score: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)


class Retriever(Protocol):
    """一个功能的召回器：**只管怎么召，不管取几条**。"""

    source: str
    """来源名（与 :class:`RetrievalPlan` 的 ``source`` 一致）。"""

    def search(self, query: str, *, limit: int) -> list[Hit]:
        """返回按本路规则排好序的候选（最多 ``limit`` 条）。

        参数：
            query: 用户查询原文。
            limit: 要几条（由 plan 的 ``candidate_k`` 决定）。
        """
        ...


@dataclass(frozen=True)
class RetrievalPlan:
    """一个功能"这次怎么召回"的参数。

    参数：
        retriever: 该功能的召回器。
        candidate_k: 本路候选数（各功能不同）。
        mode: 检索模式（``"dense"`` / ``"hybrid"`` / ``"exact_first"``）——由各功能自己解释。
        weight: 融合时的**名次权重**（默认 1.0；见 :func:`app.core.retrieval.fusion.rrf_fuse`）。
        filters: 功能自己的过滤条件（user_id / enabled / doc_id…）。
        min_score: 该路自己的分数下限（``None`` = 不过滤）。
        enabled: 本次是否启用（编排层可按意图关掉某一路）。
    """

    retriever: Retriever
    candidate_k: int
    mode: str = "dense"
    weight: float = 1.0
    filters: dict[str, Any] = field(default_factory=dict)
    min_score: float | None = None
    enabled: bool = True

    @property
    def source(self) -> str:
        """来源名（从 retriever 派生，避免两处写不一致）。"""
        return self.retriever.source
