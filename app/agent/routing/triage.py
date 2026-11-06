"""L3 分诊决策：把 L1/L2 产物转成统一执行计划 `TriageDecision`（零 LLM）。

条件边只读本结果，不在边里重算。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.contracts.triage import IntentDecision, TriageDecision  # 契约

__all__ = ["AGENTIC_MARKERS", "MULTI_MARKERS", "decide_triage"]  # 导出

#: 依赖链/多跳语义信号 → agentic。
AGENTIC_MARKERS: tuple[str, ...] = (  # 因果/诊断
    "为什么", "原因", "导致", "排查", "根因", "如果", "那么", "原理", "怎么才能", "步骤", "接着",
)  # 标记

#: 多源独立信号 → fast_multi。
MULTI_MARKERS: tuple[str, ...] = ("对比", "区别", "清单", "并列", "分别", "有哪些", "各自")  # 标记


def _has(text: str, markers: tuple[str, ...]) -> bool:  # 命中任一
    return any(word in text for word in markers)  # 判断


def decide_triage(  # 纯函数：产出统一执行计划
    query: str,  # 规范化问题
    intent: IntentDecision,  # L2 意图
    flags: dict[str, Any],  # 硬约束
    *,
    escalated: bool = False,  # 本会话是否已升级过（防乒乓）
) -> TriageDecision:  # 决策
    """顺序：flags 短路 > 意图判定 > 兜底。"""
    text = str(query or "")  # 原文
    domain = intent.domain or ""  # 域
    kind = intent.intent  # 意图
    # 1) flags 硬约束（模型不可推翻）
    if flags.get("no_tools"):  # 禁工具
        return TriageDecision(intent=kind, domain=domain, path_type="agentic", agent="general_agent", escalate_reason="flag_no_tools")  # 兜底
    if flags.get("mode") == "deep_research":  # 深研
        return TriageDecision(intent="query", domain=domain, path_type="agentic", agent="knowledge_agent", escalate_reason="flag_deep")  # 深研
    if flags.get("mode") == "answer_only":  # 只要答案
        if escalated:  # 已升级过
            return TriageDecision(intent="query", domain=domain, path_type="fast_single", escalate_reason="escalate_blocked")  # 不再升级
        return TriageDecision(intent="query", domain=domain, path_type="fast_single")  # 快捷
    # 2) 意图判定
    if kind == "action":  # 操作意图（本期无写工具）
        return TriageDecision(intent="action", domain=domain, path_type="unsupported", escalate_reason="no_action_capability")  # 显式不支持
    if kind == "clarify":  # 已判定需澄清
        return TriageDecision(intent="clarify", domain=domain, path_type="clarify")  # 澄清
    # query
    if escalated:  # 防 fast→agentic→fast 乒乓
        return TriageDecision(intent="query", domain=domain, path_type="fast_single", escalate_reason="escalate_blocked")  # 锁定
    if _has(text, AGENTIC_MARKERS):  # 有依赖链
        return TriageDecision(intent="query", domain=domain, path_type="agentic", agent="knowledge_agent", escalate_reason="dependency_chain")  # 专家子图
    if _has(text, MULTI_MARKERS):  # 多源独立
        return TriageDecision(intent="query", domain=domain, path_type="fast_multi")  # 多源
    return TriageDecision(intent="query", domain=domain, path_type="fast_single")  # 单源快速