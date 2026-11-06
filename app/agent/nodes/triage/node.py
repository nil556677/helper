"""L3 分诊节点：读 L1/L2 产物 → 写 `triage`（统一执行计划）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.contracts.output import DecisionTarget, RouteDecision  # 决策
from app.agent.contracts.triage import IntentDecision, TriageDecision  # 契约
from app.agent.routing.triage import decide_triage  # 决策

__all__ = ["triage_node"]  # 导出


def _intent_of(state: dict[str, Any]) -> IntentDecision:  # 取意图
    raw = state.get("intent")  # 意图 JSON
    if isinstance(raw, dict) and raw:  # 有
        try:  # 容错
            return IntentDecision.model_validate(raw)  # 解析
        except Exception:  # 坏数据
            return IntentDecision()  # 默认（clarify）
    return IntentDecision()  # 默认


def _flags_of(state: dict[str, Any]) -> dict[str, Any]:  # 取 flags
    flags = state.get("flags")  # 顶层
    if isinstance(flags, dict) and flags:  # 有
        return flags  # 用
    pre = state.get("preprocessed")  # 预处理产物
    if isinstance(pre, dict) and isinstance(pre.get("flags"), dict):  # 从产物取
        return pre["flags"]  # 用
    return {}  # 空


def triage_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """零 LLM：短路 + 意图判定 → TriageDecision；条件边直读本对象。"""
    del config  # 不用图配置
    pre = state.get("preprocessed") or {}  # 预处理
    query = str(pre.get("query") or state.get("question") or "") if isinstance(pre, dict) else str(state.get("question") or "")  # 问题
    # 短路①：上一轮 pending_clarify 且用户输入匹配选项 → 直接回到该固定路径
    pending = state.get("pending_clarify")  # 待澄清
    if isinstance(pending, dict) and isinstance(pre, dict) and pre.get("phase") == "followup":  # 跟进轮
        choices = pending.get("choices") if isinstance(pending.get("choices"), dict) else {}  # 选项映射
        for label, route_name in choices.items():  # 逐项
            if label and label in query:  # 命中
                decision = TriageDecision(intent="query", path_type="fast_single", escalate_reason="clarify_resolved")  # 直接
                resolved = RouteDecision(  # 直接路由
                    next=DecisionTarget.ANSWER,  # 兼容字段
                    intent="direct",  # 直接
                    route=str(route_name),  # 路径
                    confidence=1.0,  # 用户已确认
                    reason="clarify_resolved",  # 依据
                    decided_by="config",  # 用户选择
                )  # 结束
                return {"triage": decision.model_dump(mode="json"), "route": resolved.model_dump(mode="json")}  # 短路
    decision = decide_triage(  # 决策
        query,  # 问题
        _intent_of(state),  # 意图
        _flags_of(state),  # 硬约束
        escalated=bool(state.get("escalated_this_session")),  # 乒乓防护
    )  # 结束
    return {"triage": decision.model_dump(mode="json")}  # 写回