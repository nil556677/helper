"""M4 每计划独立预算：PlanBudget 独立、耗尽判断、snapshot。"""

from __future__ import annotations  # 延迟值注解

from app.agent.fallback.budget import PlanBudget  # 预算


def test_initial_no_exceed() -> None:  # 初始未超
    b = PlanBudget()  # 新建
    assert b.exceeded() is None  # 未超


def test_steps_exceed() -> None:  # 步数超
    b = PlanBudget(max_steps=2)  # 限 2 步
    b.mark_step()  # 1
    assert b.exceeded() is None  # 未超
    b.mark_step()  # 2
    assert b.exceeded() == "budget_steps"  # 超步


def test_replans_exceed() -> None:  # 重规划超
    b = PlanBudget(max_replans=1)  # 限 1
    assert b.exceeded() is None  # 未超
    b.mark_replan()  # 1
    assert b.exceeded() == "budget_replans"  # 超


def test_tokens_exceed() -> None:  # token 超
    b = PlanBudget(max_tokens=100)  # 限 100
    b.consume_tokens(120)  # 用 120
    assert b.exceeded() == "budget_tokens"  # 超


def test_tool_calls_exceed() -> None:  # 工具超
    b = PlanBudget(max_tool_calls=1)  # 限 1
    b.consume_tool()  # 1
    b.consume_tool()  # 2
    assert b.exceeded() == "budget_tools"  # 超


def test_independent_instances() -> None:  # 两个实例互不污染
    a = PlanBudget(max_steps=1)  # A
    b = PlanBudget(max_steps=1)  # B（独立）
    a.mark_step()  # A 用掉
    assert a.exceeded() == "budget_steps"  # A 超
    assert b.exceeded() is None  # B 未超（独立）


def test_snapshot_fields() -> None:  # 快照字段齐全
    b = PlanBudget(max_steps=3)  # 预算
    b.mark_step()  # 一步
    snap = b.snapshot()  # 快照
    assert snap["used_steps"] == 1  # 用 1
    assert snap["limits"]["max_steps"] == 3  # 上限 3