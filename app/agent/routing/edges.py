"""路由条件边：只读 state，返回下一节点名（纯函数）。

`path_map` 里必须同时挂 `"fallback": END` 兜底（由装配处提供）。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

__all__ = ["after_clarify", "after_dispatch", "after_rule", "after_semantic", "after_triage"]  # 导出


def after_rule(state: dict[str, Any]) -> str:  # 规则层之后
    """规则命中（intent=direct）→ 固定路径；否则下沉语义层。"""
    route = state.get("route") or {}  # 决策 JSON
    if isinstance(route, dict) and route.get("intent") == "direct":  # 已定
        return "dispatch_fixed"  # 直接走固定路径
    return "semantic_router"  # 未定 → 语义层


def after_semantic(state: dict[str, Any]) -> str:  # 语义层之后
    """三态：direct → 固定路径；ambiguous → LLM 兜底；miss → 澄清/拒绝（不进 LLM）。"""
    recall = state.get("semantic_recall") or {}  # 召回 JSON
    decision = recall.get("decision") if isinstance(recall, dict) else None  # 三态
    if decision == "direct":  # 高置信
        return "dispatch_fixed"  # 固定路径
    if decision == "ambiguous":  # 有候选但分不出
        return "route_llm"  # 轻量 LLM 兜底
    return "clarify_or_end"  # miss → 澄清/拒绝（不进 LLM）


def after_dispatch(state: dict[str, Any]) -> str:  # 固定路径之后
    """质量不够（带升级原因）→ 兜底子图；否则去校验。"""
    return "llm_fallback" if state.get("escalate_reason") else "verify"  # 升级或校验


def after_clarify(state: dict[str, Any]) -> str:  # 澄清之后
    """resume 选固定路径→dispatch；自由输入(reroute)→重走预处理；否则收尾。"""
    route = state.get("route") or {}  # 决策 JSON
    if isinstance(route, dict) and route.get("intent") == "direct" and route.get("route"):  # 已选路径
        return "dispatch_fixed"  # 去执行
    if state.get("reroute"):  # 自由输入 → 用新问题重走全流程
        return "preprocess"  # 重新路由
    return "finalize"  # 模板澄清 → 收尾


def after_triage(state: dict[str, Any]) -> str:  # 分诊之后
    """只读 `triage`：general_agent→兜底；fast_single→固定；多源/专家→子图；其余→澄清。"""
    triage = state.get("triage") or {}  # 分诊 JSON
    if isinstance(triage, dict) and triage.get("agent") == "general_agent":  # 通用兜底
        return "general_agent"  # 兜底节点
    path = triage.get("path_type") if isinstance(triage, dict) else None  # 路径
    if path == "fast_single":  # 单源快速
        return "dispatch_fixed"  # 固定路径
    if path in ("fast_multi", "agentic"):  # 多源/专家
        return "llm_fallback"  # 兜底子图
    if path in ("fast_action", "expert_action"):  # 本期不做
        return "clarify_or_end"  # 先走澄清（显式，不静默）
    return "clarify_or_end"  # clarify / unsupported