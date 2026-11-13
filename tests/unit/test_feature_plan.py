"""M3 灰度开关：HELPER_PLAN_EXECUTE off 时复杂也回走单 ReAct。"""

from __future__ import annotations  # 延迟值注解

from app.agent.contexts.feature import plan_execute_enabled  # 开关
from app.agent.routing.edges import after_triage  # 边


def test_switch_default_on() -> None:  # 缺省 on
    assert plan_execute_enabled() is True  # 开（缺省）


def test_switch_off() -> None:  # 显式 off
    import os  # 环境

    os.environ["HELPER_PLAN_EXECUTE"] = "off"  # 关
    try:  # 恢复
        assert plan_execute_enabled() is False  # 关
    finally:  # 清理
        os.environ.pop("HELPER_PLAN_EXECUTE", None)  # 删


def test_edge_complex_on_routes_to_plan(monkeypatch) -> None:  # 开关开：复杂 → plan_execute
    monkeypatch.setenv("HELPER_PLAN_EXECUTE", "on")  # 开
    state = {"triage": {"path_type": "agentic", "complexity": "complex"}}  # 复杂
    assert after_triage(state) == "plan_execute"  # 计划+执行


def test_edge_complex_off_goes_react(monkeypatch) -> None:  # 开关关：复杂 → llm_fallback（回退）
    monkeypatch.setenv("HELPER_PLAN_EXECUTE", "off")  # 关
    state = {"triage": {"path_type": "agentic", "complexity": "complex"}}  # 复杂
    assert after_triage(state) == "llm_fallback"  # 单 ReAct


def test_edge_simple_reac(monkeypatch) -> None:  # simple 仍走 ReAct（开关无关）
    monkeypatch.setenv("HELPER_PLAN_EXECUTE", "on")  # 开
    state = {"triage": {"path_type": "agentic", "complexity": "simple"}}  # 简单
    assert after_triage(state) == "llm_fallback"  # 单 ReAct