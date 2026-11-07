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


#: 时间类词（缺 range 时记为缺参）。
TIME_WORDS: tuple[str, ...] = ("今天", "昨天", "前天", "本周", "上周", "本月", "上月", "本季度", "上季度", "今年", "去年")  # 时间词


def _has(text: str, markers: tuple[str, ...]) -> bool:  # 命中任一
    return any(word in text for word in markers)  # 判断


def _missing(query: str, intent: IntentDecision, entities: dict[str, Any], time_range: Any) -> list[str]:  # 缺参
    """操作类缺订单号；有时间词但没解析出范围 → 记缺 time_range。"""
    missing: list[str] = []  # 结果
    if intent.intent == "action" and not (entities or {}).get("order_id"):  # 操作缺单号
        missing.append("order_id")  # 缺
    if _has(str(query or ""), TIME_WORDS) and time_range is None:  # 有时间词却没范围
        missing.append("time_range")  # 缺
    return missing  # 返回


def decide_triage(  # 纯函数：产出统一执行计划
    query: str,  # 规范化问题
    intent: IntentDecision,  # L2 意图
    flags: dict[str, Any],  # 硬约束
    *,
    escalated: bool = False,  # 本会话是否已升级过（防乒乓）
    entities: dict[str, Any] | None = None,  # 补全后的实体
    time_range: Any = None,  # 解析出的时间范围
    last_failed: bool = False,  # 上一轮是否失败
    has_route: bool = True,  # 是否已有固定路径（规则/语义 direct 命中）
) -> TriageDecision:  # 决策
    """顺序：flags 短路 > 意图判定 > 兜底。"""
    text = str(query or "")  # 原文
    domain = intent.domain or ""  # 域
    kind = intent.intent  # 意图
    missing = _missing(text, intent, entities or {}, time_range)  # 缺参
    extras = {"missing_entities": missing, "retry_after_failure": bool(last_failed)}  # 公共字段
    # 1) flags 硬约束（模型不可推翻）
    if flags.get("no_tools"):  # 禁工具
        return TriageDecision(intent=kind, domain=domain, path_type="agentic", agent="general_agent", escalate_reason="flag_no_tools", **extras)  # 兜底
    if flags.get("mode") == "deep_research":  # 深研
        return TriageDecision(intent="query", domain=domain, path_type="agentic", agent="knowledge_agent", escalate_reason="flag_deep", **extras)  # 深研
    if flags.get("mode") == "answer_only":  # 只要答案
        if escalated:  # 已升级过
            return TriageDecision(intent="query", domain=domain, path_type="fast_single", escalate_reason="escalate_blocked", **extras)  # 不再升级
        return TriageDecision(intent="query", domain=domain, path_type="fast_single", **extras)  # 快捷
    # 2) 意图判定
    if kind == "action":  # 操作意图（本期无写工具）
        return TriageDecision(intent="action", domain=domain, path_type="unsupported", escalate_reason="no_action_capability", **extras)  # 显式不支持
    if kind == "clarify":  # 需澄清 → 交给兜底 ReAct（由 agent 追问或诚实说明），不再弹窗
        return TriageDecision(intent="query", domain=domain, path_type="agentic", agent="knowledge_agent", escalate_reason="clarify_by_agent", **extras)  # agent 承担澄清
    # query
    if not has_route and not escalated:  # 没有固定路径可直连 → 交给 agentic 专家
        return TriageDecision(intent="query", domain=domain, path_type="agentic", agent="knowledge_agent", escalate_reason="no_fixed_path", **extras)  # 专家子图
    if escalated:  # 防 fast→agentic→fast 乒乓
        return TriageDecision(intent="query", domain=domain, path_type="fast_single", escalate_reason="escalate_blocked", **extras)  # 锁定
    if _has(text, AGENTIC_MARKERS):  # 有依赖链
        return TriageDecision(intent="query", domain=domain, path_type="agentic", agent="knowledge_agent", escalate_reason="dependency_chain", **extras)  # 专家子图
    if _has(text, MULTI_MARKERS):  # 多源独立
        return TriageDecision(intent="query", domain=domain, path_type="fast_multi", **extras)  # 多源
    return TriageDecision(intent="query", domain=domain, path_type="fast_single", **extras)  # 单源快速