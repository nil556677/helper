"""路由图：语义 miss 不再走 clarify 弹窗，一律交给 ReAct（不暂停）。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 跑

import uuid  # 唯一线程

import app.agent.agent as agent_mod  # 图
from app.agent.checkpoint import build_async_saver  # 真实 saver


async def _run(tmp_path) -> dict:  # 跑一轮
    async def noop_finalize(state, config=None):  # 打桩收尾
        del config  # 不用
        return {}  # 不落库

    async def fake_semantic(state, config=None):  # 强制 miss（即便有候选也不弹窗）
        del state, config  # 不用
        return {"semantic_recall": {"decision": "miss", "candidates": [{"route": "time", "score": 0.26}], "top1": 0.26, "margin": 0.0}}  # 召回

    agent_mod.finalize_node = noop_finalize  # 换收尾
    agent_mod.semantic_node = fake_semantic  # 换语义
    saver = await build_async_saver(path=tmp_path / "cp.sqlite")  # 每次独立 saver
    graph = agent_mod.build_route_graph(checkpointer=saver)  # 带检查点编译
    thread_id = f"hitl-{uuid.uuid4().hex[:8]}"  # 唯一线程
    cfg = {"configurable": {"thread_id": thread_id}}  # 线程
    initial = {"question": "随便说点什么", "conversation_id": "c", "hitl_enabled": True, "run_id": "r1", "thread_id": thread_id, "turns": [], "attachments": []}  # 输入
    return await graph.ainvoke(initial, cfg)  # 一次跑到底


def test_route_graph_miss_goes_react_without_interrupt(tmp_path) -> None:  # miss → ReAct，不暂停
    out = asyncio.run(_run(tmp_path))  # 跑
    assert "__interrupt__" not in out  # 不再为选择暂停
    # 语义 miss → triage agentic → ReAct(llm_fallback 无模型=降级话术)
    assert str(out.get("answer") or "")  # 至少给了话术（降级）