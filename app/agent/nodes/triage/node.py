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
        entities=_entities_of(pre),  # 补全后的实体
        time_range=pre.get("time_range") if isinstance(pre, dict) else None,  # 时间范围
        last_failed=str(state.get("last_turn_status") or "") == "failed",  # 上轮失败
        has_route=bool(state.get("route")),  # 是否已有固定路径
    )  # 结束
    return {"triage": _with_approval(decision)}  # 写回（补审批标记）


def _with_approval(decision: TriageDecision) -> dict[str, Any]:  # 补 approval_required
    """按目标专家的工具 mutation 判定是否需要审核（无专家则 False）。"""
    payload = decision.model_dump(mode="json")  # 序列化
    if not decision.agent:  # 无目标专家
        return payload  # 原样
    try:  # 取专家工具
        from app.agent.agents.experts import EXPERTS  # 注册表
        from app.agent.routing.approval import requires_approval  # 判定

        entry = EXPERTS.get(decision.agent)  # 专家
        tools = entry[0].tools if entry is not None else ()  # 工具名
        payload["approval_required"] = requires_approval(tools)  # 判定
    except Exception:  # 判定失败不阻断
        payload["approval_required"] = False  # 保守
    return payload  # 返回


def _entities_of(pre: Any) -> dict[str, Any]:  # 合并显式/继承实体
    if not isinstance(pre, dict):  # 没有预处理
        return {}  # 空
    bundle = pre.get("entities")  # 实体包
    if not isinstance(bundle, dict):  # 没有
        return {}  # 空
    inherited = bundle.get("inherited") if isinstance(bundle.get("inherited"), dict) else {}  # 继承
    explicit = bundle.get("explicit") if isinstance(bundle.get("explicit"), dict) else {}  # 显式
    return {**inherited, **explicit}  # 显式覆盖继承