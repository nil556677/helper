"""HITL 恢复入口：从暂停处用 Command(resume) 继续（真实 AsyncSqliteSaver）。"""

from __future__ import annotations  # 延迟求值注解

import uuid  # 唯一线程

from langgraph.graph import END, START, StateGraph  # 图

from app.agent.checkpoint import build_async_saver  # 真实 saver
from app.agent.nodes.clarify.interrupt import clarify_interrupt_node  # 节点
from app.agent.resume import interrupt_payload, interrupted, resume  # 被测


def _build(saver):  # 两节点图
    graph = StateGraph(dict)  # 图
    graph.add_node("clarify", clarify_interrupt_node)  # 澄清
    graph.add_edge(START, "clarify")  # 入口
    graph.add_edge("clarify", END)  # 出口
    return graph.compile(checkpointer=saver)  # 编译


async def test_resume_continues_paused_run(tmp_path) -> None:  # 恢复
    saver = await build_async_saver(path=tmp_path / "cp.sqlite")  # saver
    graph = _build(saver)  # 图
    thread_id = f"r-{uuid.uuid4().hex[:8]}"  # 线程
    cfg = {"configurable": {"thread_id": thread_id}}  # 配置
    paused = await graph.ainvoke(  # 跑
        {"question": "q", "hitl_enabled": True, "clarify_attempt": 0, "semantic_recall": {"candidates": [{"route": "time", "score": 0.9}]}},  # 输入
        cfg,  # 配置
    )  # 结束
    assert interrupted(paused)  # 已暂停
    payload = interrupt_payload(paused)  # 负载
    assert payload and payload["kind"] == "choose"  # 可渲染
    out = await resume(graph, thread_id, "time")  # 恢复
    assert out["route"]["route"] == "time"  # 落到路径
    assert not interrupted(out)  # 不再暂停


def test_interrupted_on_plain_result() -> None:  # 非暂停
    assert interrupted({}) is False  # 普通结果
    assert interrupt_payload({}) is None  # 无负载