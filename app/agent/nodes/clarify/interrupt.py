"""HITL 澄清节点：LangGraph 原生 `interrupt()` 暂停 → `Command(resume)` 恢复。

仅当 `state["hitl_enabled"]` 为真才暂停（生产图带 checkpointer 时开启）；
否则退化为模板化澄清，保证无 checkpointer 的调用/测试不被阻塞。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from langgraph.types import interrupt  # 原生暂停/恢复

from app.agent.contracts.output import DecisionTarget, RouteDecision  # 决策
from app.agent.contracts.routing import ClarifyOptions, InterruptPayload  # 契约
from app.agent.nodes.clarify.node import build_options, clarify_node, clarify_text, resolve_choice  # 复用

__all__ = ["clarify_interrupt_node"]  # 导出


def _recall(state: dict[str, Any]) -> dict[str, Any]:  # 取语义召回
    value = state.get("semantic_recall")  # 字段
    return value if isinstance(value, dict) else {}  # 兜底空


def clarify_interrupt_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """需要用户选择时 `interrupt(payload)`；恢复值作为选择，映射回路由。"""
    if not state.get("hitl_enabled"):  # 未启用 HITL
        return clarify_node(state, config)  # 退化为模板澄清
    attempt = int(state.get("clarify_attempt") or 0) + 1  # 第几次澄清
    options = build_options(_recall(state), attempt)  # 分档
    payload = InterruptPayload(  # 待办负载（前端只渲染）
        run_id=str(state.get("run_id") or ""),  # run
        thread_id=str(state.get("thread_id") or ""),  # 线程
        node="clarify_or_end",  # 节点
        kind="choose",  # 待选
        title="需要确认",  # 标题
        description=clarify_text(options),  # 说明
        options=[{"value": str(item.id), "label": str(item.text)} for item in options.options],  # 选项
        risk="low",  # 低风险
    ).model_dump(mode="json")  # 序列化
    choice = interrupt(payload)  # 暂停；恢复时拿到选择
    route_name = resolve_choice(str(choice or ""), options)  # 选择 → route
    if route_name:  # 命中候选
        decision = RouteDecision(  # 直接路由该固定路径
            next=DecisionTarget.ANSWER,  # 兼容字段
            intent="direct",  # 直接
            route=route_name,  # 路径
            confidence=1.0,  # 用户已确认
            reason="clarify_resolved",  # 依据
            decided_by="config",  # 用户选择
        )  # 决策结束
        return {"route": decision.model_dump(mode="json"), "clarify_attempt": attempt}  # 写回
    pending = {"choices": {str(item.text): str(item.id) for item in options.options}}  # 记录可接住的选项
    return {  # 自由输入 → 请重述，并留下 pending_clarify
        "question": str(choice or ""),  # 新问题
        "route": None,  # 未定
        "clarify_attempt": attempt,  # 次数
        "pending_clarify": pending,  # 下一轮 phase=followup 可短路
        "answer": "请重新描述你的需求，或选择上面的选项。",  # 提示
    }  # 返回