"""聊天流异常路径：启动失败、执行失败、取消、取消前停止。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 事件循环

import pytest  # 测试框架

from app.agent import stream as stream_mod  # 流式入口
from app.agent.context import RunCancelled  # 取消异常
from app.agent.events import EventType  # 事件
from app.agent.fsm.states import RunState  # 状态
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect  # 连接


@pytest.fixture(autouse=True)
def _db(fake_root):  # 隔离库
    """每个用例一个空库。"""
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关闭


class _Graph:  # 假图
    """只按需抛异常，不跑真实节点。"""

    def __init__(self, error: BaseException | None = None) -> None:  # 构造
        self.error = error  # 待抛异常

    async def ainvoke(self, state: dict, config: dict) -> dict:  # 入口
        """有异常就抛，否则正常返回。"""
        if self.error is not None:  # 要失败
            raise self.error  # 抛出
        return {}  # 正常

    async def astream(self, state: dict, config: dict, *, stream_mode=None):  # 流式入口
        """driver 现在用 astream；有异常就抛，否则产一条 values。"""
        del stream_mode  # 不在乎模式
        if self.error is not None:  # 要失败
            raise self.error  # 抛出
        yield ("values", {})  # 一条完整快照


def _run(monkeypatch, graph: _Graph, *, cancel_first: bool = False) -> tuple[list, object]:  # 跑一轮
    """启动流并收完事件。"""
    async def main() -> tuple[list, object]:  # 异步壳
        """在事件循环里启动并消费。"""
        monkeypatch.setattr(stream_mod, "_graph", lambda: graph)  # 替换图
        handle = stream_mod.start_stream("问题", session="edge")  # 启动
        events: list = []  # 事件
        if cancel_first:  # 启动前取消
            handle.cancel()  # 置位
        while True:  # 循环
            event = await handle.events.get()  # 取
            if event is stream_mod.SENTINEL:  # 结束
                break  # 退出
            events.append(event)  # 记录
        return events, handle.ctx  # 返回

    return asyncio.run(main())  # 执行


def test_graph_error_emits_error_and_failed(fake_root, monkeypatch) -> None:  # 图失败
    """异常 → error 事件 + FAILED。"""
    events, ctx = _run(monkeypatch, _Graph(RuntimeError("boom")))  # 跑
    assert any(event.type is EventType.ERROR for event in events)  # 有错误
    assert ctx.machine.state is RunState.FAILED  # 状态失败
    assert ctx.persisted is True  # 已落用户消息


def test_graph_cancel_emits_cancelled(fake_root, monkeypatch) -> None:  # 图内取消
    """RunCancelled → cancelled + CANCELLED。"""
    events, ctx = _run(monkeypatch, _Graph(RunCancelled("stop")))  # 跑
    assert any(event.type is EventType.CANCELLED for event in events)  # 有取消
    assert ctx.machine.state is RunState.CANCELLED  # 终态


def test_cancel_before_start(fake_root, monkeypatch) -> None:  # 启动前取消
    """按钮在任务开始前被点 → 仍收敛为 CANCELLED。"""
    events, ctx = _run(monkeypatch, _Graph(), cancel_first=True)  # 跑
    assert any(event.type is EventType.CANCELLED for event in events)  # 有取消
    assert ctx.machine.state is RunState.CANCELLED  # 终态


class _SlowGraph:  # 慢图
    """阻塞在 sleep 上，用来验证任务级取消。"""

    async def ainvoke(self, state: dict, config: dict) -> dict:  # 入口
        """睡很久；取消应把它打断。"""
        await asyncio.sleep(10)  # 长阻塞
        return {}  # 正常（到不了）

    async def astream(self, state: dict, config: dict, *, stream_mode=None):  # 流式入口
        """睡很久；取消应把它打断。"""
        del stream_mode  # 不在乎模式
        await asyncio.sleep(10)  # 长阻塞
        yield ("values", {})  # 正常（到不了）


def test_cancel_mid_run_cancels_task(fake_root, monkeypatch) -> None:  # 跑动中取消
    """任务已开始后取消 → cancel 任务、收敛 CANCELLED、不出现 ERROR/其它终态。"""
    async def main() -> tuple[list, object]:  # 异步壳
        """启动、等任务开始、取消、收事件。"""
        monkeypatch.setattr(stream_mod, "_graph", lambda: _SlowGraph())  # 换慢图
        handle = stream_mod.start_stream("问题", session="mid")  # 启动
        while not handle.started:  # 等任务真正开跑
            await asyncio.sleep(0)  # 让出
        handle.cancel()  # 取消
        events: list = []  # 事件
        while True:  # 收
            event = await handle.events.get()  # 取
            if event is stream_mod.SENTINEL:  # 结束
                break  # 退出
            events.append(event)  # 记录
        assert handle.task is not None and handle.task.done()  # 任务已结束
        return events, handle.ctx  # 返回

    events, ctx = asyncio.run(main())  # 执行
    assert any(event.type is EventType.CANCELLED for event in events)  # 有取消终态
    assert not any(event.type is EventType.ERROR for event in events)  # 不混错误
    assert ctx.machine.state is RunState.CANCELLED  # 状态收敛
    assert ctx.persisted is True  # 已落库（至少用户消息）
