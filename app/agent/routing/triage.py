"""L3 分诊决策：把 L1/L2 产物转成统一执行计划 `TriageDecision`（零 LLM）。

条件边只读本结果，不在边里重算。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.contracts.triage import IntentDecision, TriageDecision  # 契约

__all__ = ["AGENTIC_MARKERS", "MULTI_MARKERS", "agent_for_domain", "decide_triage"]  # 导出

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


def agent_for_domain(domain: str) -> str:  # domain → 专家（EXPERTS 查表，零 LLM）
    """`domain` 映射到已注册专家；查不到兜底 knowledge_agent，再兜底第一个。"""
    from app.agent.agents.experts import EXPERTS  # 单一注册表（延迟导入避免环）

    key = str(domain or "")  # 域
    if not EXPERTS:  # 没注册
        return "knowledge_agent"  # 默认
    for candidate in (f"{key}_agent", key):  # 先试 xxx_agent / xxx
        if candidate in EXPERTS:  # 命中
            return candidate  # 返回
    if "knowledge_agent" in EXPERTS:  # 兜底
        return "knowledge_agent"  # 通用知识
    return next(iter(EXPERTS))  # 再兜底第一个


def _missing(query: str, intent: IntentDecision, entities: dict[str, Any], time_range: Any) -> list[str]:  # 缺参
    """操作类缺订单号；有时间词但没解析出范围 → 记缺 time_range。"""
    missing: list[str] = []  # 结果
    if intent.intent == "action" and not (entities or {}).get("order_id"):  # 操作缺单号
        missing.append("order_id")  # 缺
    if _has(str(query or ""), TIME_WORDS) and time_range is None:  # 有时间词却没范围
        missing.append("time_range")  # 缺
    return missing  # 返回


def decide_triage(  # 纯函数：极简统一执行计划
    query: str,  # 规范化问题
    intent: IntentDecision,  # L2 意图
    flags: dict[str, Any],  # 硬约束
    *,
    escalated: bool = False,  # 兼容旧签名（不再使用）
    entities: dict[str, Any] | None = None,  # 兼容旧签名
    time_range: Any = None,  # 兼容旧签名
    last_failed: bool = False,  # 上一轮是否失败
    has_route: bool = True,  # 兼容旧签名
    dependency_agentic: bool = False,  # 兼容旧签名
) -> TriageDecision:  # 决策
    """极简：能接住的功能由 pretriage/规则直连；**到这里=没被接住 → 一律 ReAct**。

    不再判别 fast_single / fast_multi / dependency / unsupported——动作/澄清/复杂
    全交给 ReAct 统一处理（ReAct 未来承载更多能力）。仅 flags.no_tools 走 general。
    """
    domain = intent.domain or ""  # 域
    kind = intent.intent  # 意图
    extras = {"retry_after_failure": bool(last_failed)}  # 公共字段
    del escalated, entities, time_range, has_route, dependency_agentic  # 忽略不用的旧参数
    if flags.get("no_tools"):  # 禁工具（硬约束）
        return TriageDecision(intent=kind, domain=domain, path_type="agentic", agent="general_agent", escalate_reason="flag_no_tools", **extras)  # 通用兜底
    # 其余全部 → ReAct（唯一出口）
    return TriageDecision(intent=kind, domain=domain, path_type="agentic", agent=agent_for_domain(domain), escalate_reason="react_unified", **extras)  # ReAct