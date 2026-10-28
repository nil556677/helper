"""功能测试：过程树（串行/并行/嵌套）。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.agents.tree import build_agent_tree  # 被测


def _step(agent: str, branch: str, parent: str, title: str) -> dict:  # 造一条步骤事件
    return {"agent_id": agent, "branch": branch, "parent_run_id": parent, "stage_id": f"{branch}.{title}", "title": title}  # 步骤


def test_main_steps_excluded() -> None:  # 主链路
    """没有 agent 维度的步骤不进树。"""
    assert build_agent_tree([{"stage_id": "main", "title": "准备对话"}]) == []  # 空树


def test_serial_tree_single_root() -> None:  # 单委派
    """一个子 agent = 一个根节点，步骤按顺序。"""
    roots = build_agent_tree([_step("kb_agent", "b1", "run-parent", "查找资料"), _step("kb_agent", "b1", "run-parent", "生成回答")])  # 建树
    assert len(roots) == 1  # 一个根
    assert roots[0].agent_id == "kb_agent"  # agent
    assert [s["title"] for s in roots[0].steps] == ["查找资料", "生成回答"]  # 顺序


def test_parallel_branches_two_roots() -> None:  # 并行
    """同父多路 = 多个根（branch 区分）。"""
    roots = build_agent_tree([_step("kb_agent", "b1", "p", "查"), _step("file_agent", "b2", "p", "读")])  # 建树
    assert sorted(node.key for node in roots) == ["b1", "b2"]  # 两路


def test_nested_child_tree() -> None:  # 嵌套
    """子 agent 的父 branch 指向父节点时挂成 children。"""
    roots = build_agent_tree([_step("kb_agent", "b1", "", "查"), _step("file_agent", "b2", "b1", "读")])  # 建树
    assert len(roots) == 1  # 一个根
    assert roots[0].key == "b1"  # 根
    assert [child.key for child in roots[0].children] == ["b2"]  # 子节点
