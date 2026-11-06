"""HITL：clarify 的 interrupt() 暂停与 Command(resume) 恢复（真实 AsyncSqliteSaver）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, TypedDict  # 状态

from langgraph.graph import END, START, StateGraph  # 图
from langgraph.types import Command  # 恢复命令

from app.agent.checkpoint import build_async_saver  # 真实 saver
from app.agent.nodes.clarify.interrupt import clarify_interrupt_node  # 被测节点


class _S(TypedDict, total=False):  # 最小状态
    question: str  # 问题
    semantic_recall: dict[str, Any]  # 召回
    hitl_enabled: bool  # 是否启用
    clarify_attempt: int  # 次数
    answer: str  # 模板回答
    route: dict[str, Any] | None  # 路由


def _graph(saver):  # 建两节点图
    g = StateGraph(_S)  # 图
    g.add_node("clarify", clarify_interrupt_node)  # 澄清
    g.add_edge(START, "clarify")  # 入口
    g.add_edge("clarify", END)  # 出口
    return g.compile(checkpointer=saver)  # 编译


async def test_interrupt_then_resume(tmp_path) -> None:  # 暂停→恢复
    saver = await build_async_saver(path=tmp_path / "cp.sqlite")  # 真实 saver
    graph = _graph(saver)  # 图
    cfg = {"configurable": {"thread_id": "t1"}}  # 线程
    state = {"question": "q", "hitl_enabled": True, "clarify_attempt": 0, "semantic_recall": {"candidates": [{"route": "time", "score": 0.9}]}}  # 输入
    first = await graph.ainvoke(state, cfg)  # 第一次
    assert "__interrupt__" in first  # 已暂停
    payload = first["__interrupt__"][0].value  # 负载
    assert payload["kind"] == "choose" and payload["options"]  # 有选项
    resumed = await graph.ainvoke(Command(resume="time"), cfg)  # 恢复并选择
    assert resumed["route"]["route"] == "time"  # 落到固定路径
    assert resumed["route"]["intent"] == "direct"  # 直接


async def test_no_hitl_degrades_to_template(tmp_path) -> None:  # 未启用则模板澄清
    saver = await build_async_saver(path=tmp_path / "cp2.sqlite")  # saver
    graph = _graph(saver)  # 图
    cfg = {"configurable": {"thread_id": "t2"}}  # 线程
    out = await graph.ainvoke({"question": "q", "hitl_enabled": False, "clarify_attempt": 0, "semantic_recall": {"candidates": []}}, cfg)  # 跑
    assert "__interrupt__" not in out  # 不暂停
    assert out.get("answer")  # 模板话术