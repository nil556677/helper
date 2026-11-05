"""意图路由契约：单源枚举、描述符校验、扩展字段默认向后兼容。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.agents.experts import EXPERT_KINDS, assert_experts_complete, discover_experts  # 发现器
from app.agent.contracts import (  # 契约
    AGENT_KINDS,
    SIDE_EFFECTS,
    AgentDescriptor,
    ClarifyOptions,
    InterruptPayload,
    ResumeCommand,
    RouteCandidate,
    RouteDecision,
    SemanticRecall,
)


def test_enums_single_source() -> None:  # 枚举单源
    assert "unsupported" in AGENT_KINDS  # 出口标记在
    assert "unsupported" not in EXPERT_KINDS  # 专家列表不含出口
    assert set(SIDE_EFFECTS) == {"none", "reversible", "irreversible"}  # 副作用三档


def test_route_decision_backward_compatible() -> None:  # 旧构造方式仍可用
    decision = RouteDecision(next="answer", reason="x")  # 只给旧字段
    assert decision.intent == "fallback"  # 新字段有默认
    assert decision.route is None  # 默认空
    assert decision.tools == ()  # 默认空
    assert decision.decided_by == "fallback"  # 默认兜底


def test_semantic_and_clarify_contracts() -> None:  # 语义/澄清契约
    recall = SemanticRecall(candidates=[RouteCandidate(route="time", score=0.99)], top1=0.99, margin=0.2, decision="direct")  # 召回
    assert recall.candidates[0].route == "time"  # 字段
    clarify = ClarifyOptions(kind="open", clarify_attempt=1)  # 开放澄清
    assert clarify.kind == "open"  # 档位


def test_hitl_contracts() -> None:  # HITL 负载/恢复
    payload = InterruptPayload(run_id="r", thread_id="t", node="execute", kind="approve", risk="high")  # 负载
    assert payload.risk == "high"  # 风险
    command = ResumeCommand(thread_id="t", decision="approve")  # 恢复
    assert command.decision == "approve"  # 决策


def test_agent_descriptor_and_completeness() -> None:  # 描述符与完整性校验
    descriptor = AgentDescriptor(name="query_agent", tools=("search_kb",))  # 合法描述符
    assert descriptor.side_effect == "none"  # 默认无副作用
    discovered = discover_experts()  # 扫描结果（三个真实专家）
    assert set(discovered) == set(EXPERT_KINDS)  # 与单源枚举一致
    assert_experts_complete(discovered, strict=True)  # 完整（不抛）
    assert discovered["query_agent"][0].tools  # 只读专家挂了工具
    assert discovered["sensitive_agent"][0].side_effect == "irreversible"  # 不可逆标记