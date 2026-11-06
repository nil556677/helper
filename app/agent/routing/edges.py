"""路由条件边：只读 state，返回下一节点名（纯函数）。

`path_map` 里必须同时挂 `"fallback": END` 兜底（由装配处提供）。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

__all__ = ["after_llm", "after_rule", "after_semantic"]  # 导出


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


def after_llm(state: dict[str, Any]) -> str:  # LLM 兜底之后
    """query/action → 兜底子图（agentic）；其余（clarify）→ 澄清节点。"""
    intent = state.get("intent") or {}  # 意图 JSON
    kind = intent.get("intent") if isinstance(intent, dict) else None  # 意图
    if kind in ("query", "action"):  # 可执行
        return "llm_fallback"  # 兜底子图
    return "clarify_or_end"  # 澄清/拒绝