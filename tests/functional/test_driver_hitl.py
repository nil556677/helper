"""driver 级：语义 miss 不再 HITL 暂停（交给 ReAct），一次跑到 done。"""

from __future__ import annotations  # 延迟值注解

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


async def _drive_miss(tmp_path, conv: str) -> tuple:  # 语义 miss 跑一次
    async def noop_finalize(state, config=None):  # 打桩收尾
        del config  # 不用
        return {}  # 不落库

    async def fake_semantic(state, config=None):  # 强制 miss
        del state, config  # 不用
        return {"semantic_recall": {"decision": "miss", "candidates": [{"route": "time", "score": 0.26}], "top1": 0.26, "margin": 0.0}}  # 召回

    agent_mod.finalize_node = noop_finalize  # 换收尾
    agent_mod.semantic_node = fake_semantic  # 换语义
    saver = await build_async_saver(path=tmp_path / "cp.sqlite")  # saver
    graph = agent_mod.build_route_graph(checkpointer=saver)  # 图
    sink = _Sink()  # 事件
    ctx = RunContext(run_id="r1", question="随便说点什么", conversation_id=conv, emit=sink)  # 上下文
    initial = {"question": "随便说点什么", "conversation_id": conv, "turns": [], "attachments": [], "hitl_enabled": True, "run_id": "r1", "thread_id": conv}  # 初始
    result = await drive(graph, ctx, initial_state=initial)  # 一次到位
    return result, sink  # 返回


async def test_driver_miss_goes_react_without_pause(tmp_path) -> None:  # miss → ReAct 不暂停
    result, sink = await _drive_miss(tmp_path, "c1")  # 跑
    assert result.status == "done"  # 结束了（不等待）
    assert str(result.state.get("answer") or "")  # 有降级话术
    assert not any(event.type is EventType.CLARIFY for event in sink.events)  # 不再推澄清弹窗