"""意图路由的**契约**（core 只给形状，不做编排）。

按统一计划：单层意图 + RAG 即工具 + L4 只转人工。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Intent(StrEnum):
    """意图枚举——**只加不改**。本期只有三类：寒暄 / 工具 / 转人工。"""

    CHITCHAT = "chitchat"
    TOOL_CALL = "tool_call"
    HUMAN_HANDOFF = "human_handoff"


@dataclass(frozen=True)
class RouteDecision:
    """路由决策：意图 + 工具 + 置信/证据 + 给 RAG 的可选 hints。"""

    intent: Intent
    tool: str = ""  # TOOL_CALL 时必填（search_kb / query_order / weather）
    confidence: float = 1.0
    evidence: str = ""  # 命中哪条规则 / 哪个范例（人话，进过程行）
    stage: str = "rules"  # rules / vector / llm
    fallback: bool = False  # True = 未命中，默认 search_kb
    slots: dict[str, str] = field(default_factory=dict)
    # 给 search_kb 的可选 hints（默认 None/空 = 不改变现状）
    rewrite: bool | None = None
    top_k: int | None = None
