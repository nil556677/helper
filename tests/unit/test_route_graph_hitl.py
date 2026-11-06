"""HITL 端到端：生产路由图 interrupt 暂停 → resume 选路径 → 固定路径作答。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 跑

import uuid  # 唯一线程

from langgraph.types import Command  # 恢复

import app.agent.agent as agent_mod  # 图
from app.agent.checkpoint import build_async_saver  # 真实 saver


async def _run(tmp_path) -> tuple[dict, dict]:  # 跑一轮
    async def noop_finalize(state, config=None):  # 打桩收尾
        del config  # 不用
        return {}  # 不落库

    async def fake_semantic(state, config=None):  # 强制 miss 但保留可展示候选 → 澄清选项
        del state, config  # 不用
        return {"semantic_recall": {"decision": "miss", "candidates": [{"route": "time", "score": 0.26}], "top1": 0.26, "margin": 0.0}}  # 召回

    agent_mod.finalize_node = noop_finalize  # 换收尾
    agent_mod.semantic_node = fake_semantic  # 换语义
    saver = await build_async_saver(path=tmp_path / "cp.sqlite")  # 每次独立 saver
    graph = agent_mod.build_route_graph(checkpointer=saver)  # 带检查点编译
    thread_id = f"hitl-{uuid.uuid4().hex[:8]}"  # 唯一线程
    cfg = {"configurable": {"thread_id": thread_id}}  # 线程
    initial = {"question": "随便说点什么", "conversation_id": "c", "hitl_enabled": True, "run_id": "r1", "thread_id": thread_id, "turns": [], "attachments": []}  # 输入
    paused = await graph.ainvoke(initial, cfg)  # 第一次：暂停
    resumed = await graph.ainvoke(Command(resume="time"), cfg)  # 恢复：选 time
    return paused, resumed  # 返回


def test_route_graph_hitl_pause_and_resume(tmp_path) -> None:  # 暂停 + 恢复
    paused, resumed = asyncio.run(_run(tmp_path))  # 跑
    assert "__interrupt__" in paused  # 暂停了
    assert resumed["route"]["route"] == "time"  # 落到固定路径
    assert str(resumed.get("answer") or "").startswith("现在是 ")  # 固定路径作答