"""driver 级 HITL：暂停置 WAITING、resume 继续到 done。"""

from __future__ import annotations  # 延迟求值注解

import pytest  # 测试框架

import app.agent.agent as agent_mod  # 图
from app.agent.checkpoint import build_async_saver  # 真实 saver
from app.agent.context import RunContext  # 上下文
from app.agent.driver import drive  # 驱动
from app.agent.events import EventType  # 事件
from app.core.db import schema_v1  # 建表
from app.core.db.connection import close_thread_connection, connect  # 连接


@pytest.fixture(autouse=True)  # 独立库
def _db(fake_root):
    """建 schema（driver 的 trace 需要）。"""
    conn = connect()  # 连接
    try:  # 建
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关
        conn.close()  # 关
    yield  # 交给用例
    close_thread_connection()  # 收尾


class _Sink:  # 事件
    def __init__(self) -> None:
        self.events: list = []  # 列表

    def __call__(self, event) -> None:  # 收
        self.events.append(event)  # 追加


async def test_driver_pauses_then_resumes(tmp_path) -> None:  # 暂停 + 恢复
    async def noop_finalize(state, config=None):  # 打桩收尾
        del config  # 不用
        return {}  # 不落库

    async def fake_semantic(state, config=None):  # 强制 miss（带可展示候选）
        del state, config  # 不用
        return {"semantic_recall": {"decision": "miss", "candidates": [{"route": "time", "score": 0.26}], "top1": 0.26, "margin": 0.0}}  # 召回

    agent_mod.finalize_node = noop_finalize  # 换收尾
    agent_mod.semantic_node = fake_semantic  # 换语义
    saver = await build_async_saver(path=tmp_path / "cp.sqlite")  # saver
    graph = agent_mod.build_route_graph(checkpointer=saver)  # 图
    sink = _Sink()  # 事件
    ctx = RunContext(run_id="r1", question="随便说点什么", conversation_id="c1", emit=sink)  # 上下文
    initial = {"question": "随便说点什么", "conversation_id": "c1", "turns": [], "attachments": [], "hitl_enabled": True, "run_id": "r1", "thread_id": "c1"}  # 初始
    paused = await drive(graph, ctx, initial_state=initial)  # 第一次
    assert paused.status == "waiting"  # 暂停
    assert ctx.interrupt_payload and ctx.interrupt_payload.get("kind") == "choose"  # 负载
    assert any(event.type is EventType.CLARIFY for event in sink.events)  # 推了澄清事件
    resumed = await drive(graph, ctx, initial_state={}, resume_value="time")  # 恢复
    assert resumed.status == "done"  # 完成
    assert resumed.state.get("route", {}).get("route") == "time"  # 落到路径
    assert str(resumed.state.get("answer") or "").startswith("现在是 ")  # 固定路径作答
    assert any(event.type is EventType.DONE for event in sink.events)  # 有终态