"""功能测试：既有 driver 能驱动新路由图（direct 路径）到 done。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

import pytest  # 测试框架

from app.agent.agent import build_route_graph  # 新父图
from app.agent.context import RunContext  # 上下文
from app.agent.driver import drive  # 驱动器
from app.agent.events import EventType  # 事件类型
from app.core.db import schema_v1  # 建表
from app.core.db.connection import close_thread_connection, connect  # 连接


@pytest.fixture(autouse=True)  # 独立库
def _db(fake_root):
    """建 v1 schema（driver 的 trace/落库需要）。"""
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关
    yield  # 交给用例
    close_thread_connection()  # 收尾


class _Sink:  # 事件收集
    def __init__(self) -> None:  # 构造
        self.events: list[Any] = []  # 事件
        self._run_id = ""  # 出口 run id

    def __call__(self, event: Any) -> None:  # 收事件
        self.events.append(event)  # 追加


def _state_path(events: list[Any]) -> list[str]:  # 状态序列
    return [event.payload.after for event in events if event.type is EventType.STATE]  # 结束


async def _run(question: str) -> tuple[Any, _Sink]:  # 跑一轮
    graph = build_route_graph()  # 新图
    sink = _Sink()  # 事件
    ctx = RunContext(run_id="r", question=question, conversation_id="c1", emit=sink)  # 上下文
    initial = {"question": question, "conversation_id": "c1", "turns": [], "attachments": []}  # 初始
    result = await drive(graph, ctx, initial_state=initial)  # 驱动
    return result, sink  # 返回


async def test_driver_drives_route_graph_direct() -> None:  # direct 路径
    result, sink = await _run("现在几点")  # 跑
    assert result.status == "done"  # 终态
    assert str(result.state.get("answer") or "").startswith("现在是 ")  # 回答
    path = _state_path(sink.events)  # 状态序列
    assert path == ["context_loading", "retrieving", "generating", "finalizing", "done"]  # 完整生命周期
    assert sink.events[-1].type is EventType.DONE  # 最后一帧 done


async def test_driver_drives_route_graph_miss_to_react() -> None:  # miss → ReAct（不再写 clarify_options）
    result, sink = await _run("帮我解释一下这个奇怪的需求，完全无关")  # 跑（易混/无候选）
    assert result.status == "done"  # 终态
    assert str(result.state.get("answer") or "")  # 有降级/诚意话术（ReAct）