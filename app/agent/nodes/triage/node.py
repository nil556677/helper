"""L3 分诊节点：读 L1/L2 产物 → 写 `triage`（统一执行计划）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.contracts.triage import IntentDecision  # 契约
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
    decision = decide_triage(  # 决策
        query,  # 问题
        _intent_of(state),  # 意图
        _flags_of(state),  # 硬约束
        escalated=bool(state.get("escalated_this_session")),  # 乒乓防护
    )  # 结束
    return {"triage": decision.model_dump(mode="json")}  # 写回