"""L3 分诊：短路/意图判定/路径与边。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.contracts.triage import IntentDecision  # 契约
from app.agent.nodes.triage.node import triage_node  # 节点
from app.agent.routing.edges import after_triage  # 边
from app.agent.routing.triage import decide_triage  # 决策


def _intent(kind: str = "query", domain: str = "knowledge") -> IntentDecision:  # 造意图
    return IntentDecision(intent=kind, domain=domain, confidence=0.9, source="rule")  # 返回


def test_query_paths() -> None:  # 查询分诊
    assert decide_triage("现在几点", _intent(), {}).path_type == "fast_single"  # 单源
    assert decide_triage("为什么会这样", _intent(), {}).path_type == "agentic"  # 依赖链
    assert decide_triage("A 和 B 有何区别", _intent(), {}).path_type == "fast_multi"  # 多源
    decided = decide_triage("为什么会这样", _intent(), {})  # 专家
    assert decided.agent == "knowledge_agent"  # 标注专家


def test_action_and_clarify() -> None:  # 操作/澄清
    assert decide_triage("退款", _intent("action"), {}).path_type == "unsupported"  # 本期无写工具
    decided = decide_triage("帮我", _intent("clarify"), {})  # 需澄清
    assert decided.path_type == "agentic"  # 澄清交给兜底 ReAct（agent 追问/诚实），不再弹窗


def test_flags_short_circuit() -> None:  # flags 硬约束
    assert decide_triage("随便", _intent(), {"no_tools": True}).agent == "general_agent"  # 禁工具
    assert decide_triage("随便", _intent(), {"mode": "answer_only"}).path_type == "fast_single"  # 只答案
    deep = decide_triage("随便", _intent(), {"mode": "deep_research"})  # 深研
    assert deep.path_type == "agentic" and deep.agent == "knowledge_agent"  # 专家


def test_escalate_guard() -> None:  # 防乒乓
    decided = decide_triage("为什么", _intent(), {}, escalated=True)  # 已升级过
    assert decided.path_type == "fast_single" and decided.escalate_reason == "escalate_blocked"  # 锁定


def test_no_fixed_path_goes_agentic() -> None:  # 无固定路径 → 专家
    decided = decide_triage("亚太共同体 是什么", _intent(), {}, has_route=False)  # 无路由
    assert decided.path_type == "agentic" and decided.agent == "knowledge_agent"  # 走专家
    assert decided.escalate_reason == "no_fixed_path"  # 原因
    with_route = decide_triage("亚太共同体 是什么", _intent(), {}, has_route=True)  # 有路由
    assert with_route.path_type == "fast_single"  # 快速


def test_missing_entities_and_retry() -> None:  # 缺参/重试字段
    action = decide_triage("退款", _intent("action"), {}, entities={})  # 操作缺单号
    assert action.missing_entities == ["order_id"]  # 缺 order_id
    with_time = decide_triage("上周怎么样", _intent(), {}, time_range=None)  # 有时间词无范围
    assert "time_range" in with_time.missing_entities  # 缺 time_range
    retried = decide_triage("问题", _intent(), {}, last_failed=True)  # 上轮失败
    assert retried.retry_after_failure is True  # 标记重试


def test_triage_node_and_edge() -> None:  # 节点 + 边
    out = triage_node(  # 跑
        {"question": "为什么这样", "preprocessed": {"query": "为什么这样"}, "intent": _intent().model_dump(), "flags": {}}  # 输入
    )  # 结束
    assert out["triage"]["path_type"] == "agentic"  # 路径
    assert after_triage(out) == "llm_fallback"  # 边
    empty = triage_node({"question": "x"})  # 无意图
    assert empty["triage"]["path_type"] == "agentic"  # 默认交给 agent（澄清由 agent 承担）