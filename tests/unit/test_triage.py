"""L3 分诊：极简 —— 没被接住的都走 ReAct。"""

from __future__ import annotations  # 延迟值注解

from app.agent.contracts.triage import IntentDecision  # 契约
from app.agent.nodes.triage.node import triage_node  # 节点
from app.agent.routing.edges import after_triage  # 边
from app.agent.routing.triage import decide_triage  # 决策


def _intent(kind: str = "query", domain: str = "knowledge") -> IntentDecision:  # 造意图
    return IntentDecision(intent=kind, domain=domain, confidence=0.9, source="rule")  # 返回


def test_everything_unhandled_goes_react() -> None:  # 没被接住一律 ReAct
    for kind in ("query", "action", "clarify"):  # 各类意图
        decided = decide_triage("随便", _intent(kind), {})  # 决策
        assert decided.path_type == "agentic"  # 一律 agentic
        assert decided.agent == "knowledge_agent"  # 知识专家（ReAct 宿主）
        assert decided.escalate_reason == "react_unified"  # 统一出口


def test_flags_no_tools_general() -> None:  # 禁工具仍走 general
    decided = decide_triage("随便", _intent(), {"no_tools": True})  # 禁工具
    assert decided.agent == "general_agent"  # 通用兜底（硬约束）
    assert decided.path_type == "agentic"  # 仍 agentic 承载


def test_retry_flag_kept() -> None:  # 上轮失败标记保留
    retried = decide_triage("问题", _intent(), {}, last_failed=True)  # 失败
    assert retried.retry_after_failure is True  # 标记


def test_triage_node_and_edge() -> None:  # 节点 + 边
    out = triage_node(  # 跑
        {"question": "为什么这样", "preprocessed": {"query": "为什么这样"}, "intent": _intent().model_dump(), "flags": {}}  # 输入
    )  # 结束
    assert out["triage"]["path_type"] == "agentic"  # 路径
    assert after_triage(out) == "llm_fallback"  # 边 → ReAct
    empty = triage_node({"question": "x"})  # 无意图
    assert empty["triage"]["path_type"] == "agentic"  # 默认一律 ReAct