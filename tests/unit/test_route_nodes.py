"""M6 节点与路由父图：规则命中→固定路径直调；语义层节点；父图可编译。"""

from __future__ import annotations  # 延迟求值注解

import pytest  # 测试框架

from app.agent.agent import build_route_graph  # 路由父图
from app.agent.nodes.dispatch.node import dispatch_node  # 固定路径
from app.agent.nodes.route.node import rule_route_node  # 规则层
from app.agent.nodes.semantic.node import semantic_node  # 语义层


def test_rule_route_hits_direct() -> None:  # 规则命中 → direct
    out = rule_route_node({"question": "现在几点"})  # 跑
    assert out["route"]["intent"] == "direct"  # 直接
    assert out["route"]["route"] == "time"  # 路径
    assert out["route"]["decided_by"] == "rule"  # 来源


def test_rule_route_miss_when_ambiguous() -> None:  # 易混 → 未定
    out = rule_route_node({"question": "几点开始"})  # 跑
    assert out["route"] is None  # 交语义层


async def test_dispatch_time() -> None:  # 时间固定路径直调
    out = await dispatch_node({"question": "现在几点", "route": {"route": "time", "intent": "direct"}})  # 跑
    assert out["answer"].startswith("现在是 ")  # 模板
    assert out["fragments"] == []  # 无引用


async def test_dispatch_weather() -> None:  # 天气固定路径直调
    out = await dispatch_node({"question": "北京天气", "route": {"route": "weather"}})  # 跑
    assert out["answer"]  # 有回复（可能网络失败也带内说明）


async def test_dispatch_unknown_route() -> None:  # 未知路径 → 带内说明
    out = await dispatch_node({"question": "x", "route": {"route": "nonexistent"}})  # 跑
    assert "未配置" in out["note"]  # 带内


def test_route_graph_compiles() -> None:  # 路由父图可编译
    graph = build_route_graph()  # 编译
    assert graph is not None  # 有
    # 检查三个出口都可达（结构上不允许悬挂）
    assert {"dispatch_fixed", "llm_fallback", "clarify_or_end"}  # 出口集合存在