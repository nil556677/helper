"""Run RPC 服务端（E0.2）：HttpClient ↔ make_server ↔ InProcessClient 往返。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 跑异步
import time  # 计时（心跳超时断言）
from collections.abc import Callable  # 可调用
from typing import Any  # 任意值

import httpx  # ASGI 客户端

from app.agent import events as agent_events  # 事件工厂
from app.agent import stream as agent_stream  # sentinel
from app.agent.events import EventType  # 事件类型
from app.agent.run_client import InProcessClient, RunInput  # 后端
from app.agent.run_client_http import HttpClient  # 被测客户端
from app.agent.run_server import make_server  # 被测服务端


class FakeHandle:  # 假句柄（与 InProcess 测试同形）
    def __init__(self, run_id: str, items: list[Any]) -> None:  # 构造
        self.run_id = run_id  # run id
        self.events: asyncio.Queue = asyncio.Queue()  # 队列
        self.cancelled = False  # 取消
        self.approvals: list[tuple[str, bool]] = []  # 审批
        for item in items:  # 预填
            self.events.put_nowait(item)  # 事件
        self.events.put_nowait(agent_stream.SENTINEL)  # 结束标记

    def cancel(self) -> None:  # 假取消
        self.cancelled = True  # 记录

    def approve(self, request_id: str, allow: bool) -> None:  # 假审批
        self.approvals.append((request_id, allow))  # 记录


def _factory(items: list[Any]) -> Callable[..., FakeHandle]:  # 假工厂
    counter = 0  # 计数

    def factory(*_args: Any, **_kw: Any) -> FakeHandle:  # 工厂
        nonlocal counter  # 改外层
        counter += 1  # 递增
        return FakeHandle(f"run-{counter}", items)  # 建

    return factory  # 返回


def _run(coro: Any) -> Any:  # 同步跑
    return asyncio.run(coro)  # 完成


def _ack() -> list[Any]:  # 两条 ack + sentinel
    return [agent_events.ack("x"), agent_events.ack("x")]  # 事件


def _client_and_backend(  # 建客户端+后端
    items: list[Any],  # 假事件
) -> tuple[HttpClient, InProcessClient, httpx.AsyncClient]:  # 三元组
    backend = InProcessClient(factory=_factory(items))  # 后端
    app = make_server(backend)  # 服务
    transport = httpx.ASGITransport(app=app)  # 传输
    aclient = httpx.AsyncClient(transport=transport, base_url="http://test")  # 客户端
    return HttpClient("http://test", client=aclient), backend, aclient  # 返回


def test_http_round_trip_stream() -> None:  # 主流程
    async def main() -> list[tuple[int, EventType]]:  # 异步主体
        client, _backend, aclient = _client_and_backend(_ack())  # 建
        try:  # 用后必关
            run_id = await client.start(RunInput(question="q", conversation_id="c"))  # 启
            got: list[tuple[int, EventType]] = []  # 收集
            async for seq, event in client.events(run_id):  # 订阅
                got.append((seq, event.type))  # 记录
            await asyncio.sleep(0)  # 让 feed 任务落地
            return got  # 返回
        finally:  # 关闭
            await aclient.aclose()  # 关

    got = _run(main())  # 跑
    assert got == [(1, EventType.ACK), (2, EventType.ACK)]  # 有序递增


def test_http_cancel_and_approve_forward() -> None:  # 控制转发
    async def main() -> tuple[bool, list[tuple[str, bool]]]:  # 异步主体
        client, backend, aclient = _client_and_backend(_ack())  # 建
        try:  # 用后必关
            run_id = await client.start(RunInput(question="q", conversation_id="c"))  # 启
            handle = backend._handles[run_id]  # 句柄
            await client.cancel(run_id)  # 取消
            await client.approve(run_id, "req1", True)  # 审批
            await asyncio.sleep(0)  # 让 feed 落地
            return handle.cancelled, handle.approvals  # 结果
        finally:  # 关闭
            await aclient.aclose()  # 关

    cancelled, approvals = _run(main())  # 跑
    assert cancelled is True  # 取消生效
    assert approvals == [("req1", True)]  # 审批转发


def test_http_resume_from_last_event_id() -> None:  # 断线续传
    async def main() -> int:  # 异步主体
        client, _backend, aclient = _client_and_backend(_ack())  # 建
        try:  # 用后必关
            run_id = await client.start(RunInput(question="q", conversation_id="c"))  # 启
            last = 0  # 上一轮已发的最大 seq
            async for seq, _event in client.events(run_id):  # 第一遍
                last = seq  # 记录
            count = 0  # 续传计数
            async for _seq, _event in client.events(run_id, last_event_id=str(last)):  # 续传
                count += 1  # 递增
            await asyncio.sleep(0)  # 落地
            return count  # 返回
        finally:  # 关闭
            await aclient.aclose()  # 关

    assert _run(main()) == 0  # 已发过的不再重发


class _PacedBackend:  # 按节奏产事件的假后端（仅用于构造 feed，不跑服务）
    async def start(self, _run_input: Any) -> str:  # 启动
        return "r-paced"  # 固定 id

    async def events(self, run_id: str, *, last_event_id: str = "") -> Any:  # 事件流
        del run_id, last_event_id  # 不用
        yield agent_events.ack("x")  # 第一条：立即

    async def cancel(self, run_id: str) -> None:  # 取消
        return None  # 空

    async def approve(self, run_id: str, request_id: str, allow: bool) -> None:  # 审批
        return None  # 空


def test_wait_for_more_does_not_busy_loop_on_set_wake(monkeypatch: Any) -> None:  # wake 已 set 不能空转
    """``_wait_for_more`` 在 wake 已 set、又没有新数据时，必须等到心跳超时才返回 False。

    回归：旧写法把 ``wake.clear()`` 放在 TimeoutError 分支里，wake 已 set 时
    ``Event.wait()`` 立即返回、不挂起 → 调用方（SSE 生成器）变成不挂起的死循环，
    占死事件循环，SSE 一个字节都发不出去。这里坏写法会 ~0s 返回 True（断言立即失败），
    不会把测试挂死。
    """
    import app.agent.run_server as rs  # 被测

    monkeypatch.setattr(rs, "_HEARTBEAT_S", 0.05)  # 缩短心跳便于测试

    async def main() -> tuple[bool, float]:  # 异步主体
        feed = rs._Feed(backend=_PacedBackend(), run_id="r")  # 建 feed（会建 Event）
        feed.wake.set()  # 模拟"刚有新数据"已置位
        started = time.perf_counter()  # 计时
        got = await rs._wait_for_more(feed, seen=0)  # 实际上没有新数据
        return got, time.perf_counter() - started  # 结果 + 耗时

    got, elapsed = _run(main())  # 跑
    assert got is False  # 没新数据 → 应让调用方发心跳
    assert elapsed >= 0.04  # 确实等过心跳（旧写法会立刻返回 True）


def test_wait_for_more_returns_true_when_data_pending() -> None:  # 已有新数据立即返回
    import app.agent.run_server as rs  # 被测

    async def main() -> bool:  # 异步主体
        feed = rs._Feed(backend=_PacedBackend(), run_id="r")  # 建 feed
        feed.seq = 1  # 模拟已产出一条
        feed.buf.append((1, agent_events.ack("x")))  # 入缓冲
        feed.wake.set()  # 有数据信号
        return await rs._wait_for_more(feed, seen=0)  # 应立刻 True

    assert _run(main()) is True  # 有新数据
