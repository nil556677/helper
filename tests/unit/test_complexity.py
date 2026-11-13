"""M1 复杂度判定：judge 强规则 + 中性档采信；decide_triage 落复杂度。"""

from __future__ import annotations  # 延迟值注解

from app.agent.contracts.triage import IntentDecision  # 契约
from app.agent.routing.complexity import DEFAULT_CONFIDENCE_THRESHOLD, judge  # 判定
from app.agent.routing.triage import decide_triage  # 决策


def test_judge_deep_research_complex() -> None:  # 深研强制复杂
    assert judge("天气", flags={"mode": "deep_research"}) == "complex"  # 强制


def test_judge_complex_markers_complex() -> None:  # 复杂/多跳标记 → 复杂
    for q in ("为什么这个会报警", "某故障的根因是什么", "这件事的原因在哪里", "如何实现这个功能"):
        assert judge(q) == "complex", q  # 命中即 complex


def test_judge_multi_source_complex() -> None:  # 多源/对比 → 复杂
    for q in ("比较一下两个方案的区别", "分别列出优点和缺点", "这份清单有哪些内容"):
        assert judge(q) == "complex", q  # 复杂


def test_judge_long_complex() -> None:  # 长问题 → 复杂
    long = "我想了解一下这个系统里面关于知识库检索和天气查询以及订单物流这些功能各自是怎么使用的请详细说明一下"  # 长句
    assert judge(long) == "complex"  # 超阈值


def test_judge_simple() -> None:  # 简单问题 → simple
    for q in ("现在几点了", "帮我查北京今天天气", "你好"):
        assert judge(q) == "simple", q  # 简单


def test_judge_neutral_model_high_conf() -> None:  # 中性档模型高置信采信
    assert judge("x", intent_complexity="complex", intent_confidence=0.9) == "complex"  # 采信
    assert judge("x", intent_complexity="complex", intent_confidence=0.3) == "simple"  # 低置信不信


def test_judge_threshold_constant() -> None:  # 阈值常量
    assert 0.0 < DEFAULT_CONFIDENCE_THRESHOLD < 1.0  # 合理


def test_decide_triage_complex_sets_field() -> None:  # 复杂问题落复杂度字段
    decided = decide_triage("为什么订单会失败", IntentDecision(intent="query", confidence=0.9), {})  # 复杂
    assert decided.complexity == "complex"  # 复杂
    assert decided.escalate_reason == "complex_plan"  # 归因
    assert decided.path_type == "agentic"  # 仍 agentic 承载


def test_decide_triage_simple_keeps_react() -> None:  # 简单问题仍走 ReAct（兼容现状）
    decided = decide_triage("现在几点了", IntentDecision(intent="query", confidence=0.9), {})  # 简单
    assert decided.complexity == "simple"  # 简单
    assert decided.escalate_reason == "react_unified"  # 归因不变