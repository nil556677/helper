"""RunClient（E0.1）：InProcessClient 转发与事件流。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 队列/跑异步
from collections.abc import Callable  # 可调用类型
from typing import Any  # 任意值

from app.agent import stream as agent_stream  # sentinel
from app.agent.run_client import InProcessClient, RunInput  # 被测


class FakeHandle:  # 假句柄
    def __init__(self, run_id: str) -> None:  # 构造
        self.run_id = run_id  # run id
        self.events: asyncio.Queue = asyncio.Queue()  # 事件队列
        self.cancelled = False  # 是否取消
        self.approvals: list[tuple[str, bool]] = []  # 审批调用记录

    def cancel(self) -> None:  # 假取消
        self.cancelled = True  # 记录

    def approve(self, request_id: str, allow: bool) -> None:  # 假审批
        self.approvals.append((request_id, allow))  # 记录


def _make_factory(items: list[Any], record: list[Any]) -> Callable:  # 返回工厂并回填
    """造一个 start_stream 假工厂；把收到的参数写进 record。"""
    counter = 0  # 局部计数

    def factory(*args: Any, **kw: Any) -> FakeHandle:  # 假工厂
        nonlocal counter  # 改外层计数
        counter += 1  # 递增
        handle = FakeHandle(f"run-{counter}")  # 建句柄
        for item in items:  # 逐个入队
            handle.events.put_nowait(item)  # 塞事件
        handle.events.put_nowait(agent_stream.SENTINEL)  # 结束标记
        record.append((args, kw))  # 记录参数
        return handle  # 返回

    return factory  # 返回


def _run(coro) -> Any:  # 同步跑
    return asyncio.run(coro)  # 完成


def test_start_returns_run_id_and_passes_through_input() -> None:  # 起始
    calls: list[Any] = []  # 参数记录
    client = InProcessClient(factory=_make_factory([], calls))  # 客户端
    run_id = _run(client.start(RunInput(question="q", conversation_id="c1", attachments=[{"id": "a1"}])))  # 启动
    assert callable(run_id) is False and isinstance(run_id, str)  # run id
    assert calls  # 收到调用
    args, kw = calls[0]  # 解包
    assert args[0] == "q"  # 问题
    assert kw["session"] == "c1"  # 会话
    assert kw["attachments"] == [{"id": "a1"}]  # 附件全量透传


def test_events_yield_until_sentinel() -> None:  # 事件流
    msg = object()  # 假事件
    client = InProcessClient(factory=_make_factory([msg], []))  # 客户端
    run_id = _run(client.start(RunInput(question="q", conversation_id="c")))  # 启动

    async def collect() -> list[Any]:  # 收集
        out: list[Any] = []  # 结果
        async for ev in client.events(run_id):  # 迭代
            out.append(ev)  # 追加
        return out  # 返回

    assert _run(collect()) == [msg]  # 只产出不含 sentinel


def test_events_unknown_run_id_ends() -> None:  # 未知 run
    client = InProcessClient(factory=_make_factory([], []))  # 客户端

    async def count() -> int:  # 计数
        n = 0  # 计数
        async for _ev in client.events("nope"):  # 迭代
            n += 1  # 递增
        return n  # 返回

    assert _run(count()) == 0  # 立即结束


def test_cancel_and_approve_forward() -> None:  # 控制转发
    calls: list[Any] = []  # 参数
    client = InProcessClient(factory=_make_factory([], calls))  # 客户端
    run_id = _run(client.start(RunInput(question="q", conversation_id="c")))  # 启动
    handle = client._handles[run_id]  # 句柄
    _run(client.cancel(run_id))  # 取消
    assert handle.cancelled is True  # 转发到
    _run(client.approve(run_id, "req1", True))  # 审批
    assert handle.approvals == [("req1", True)]  # 记录
