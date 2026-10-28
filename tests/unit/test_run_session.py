"""UI 运行会话（E0.3）：RunSession 把 RunClient 包装成队列句柄。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 队列/跑异步
from typing import Any  # 任意值

from app.agent import events as agent_events  # 事件
from app.agent.run_session import SENTINEL, RunSession  # 被测


#: 假 RunClient：start/events/cancel/approve 前铺好。
class FakeClient:  # 假客户端
    def __init__(self, items: list[Any]) -> None:  # 构造
        self.items = items  # 事件序列
        self.cancelled: list[str] = []  # 取消记录
        self.approved: list[tuple[str, str, bool]] = []  # 审批记录

    async def start(self, _run_input: Any) -> str:  # 启动
        return "run-1"  # 固定 id

    async def events(self, run_id: str, *, last_event_id: str = "") -> Any:  # 事件流
        del run_id, last_event_id  # 不用
        for seq, ev in enumerate(self.items, start=1):  # 逐条
            yield seq, ev  # 产出

    async def cancel(self, run_id: str) -> None:  # 取消
        self.cancelled.append(run_id)  # 记录

    async def approve(self, run_id: str, request_id: str, allow: bool) -> None:  # 审批
        self.approved.append((run_id, request_id, allow))  # 记录


#: 裸事件假客户端：events 产出裸 Event（模拟 InProcessClient.events），不是 (seq, event) 元组。
class BareClient:  # 裸事件客户端
    def __init__(self, items: list[Any]) -> None:  # 构造
        self.items = items  # 事件序列

    async def start(self, _run_input: Any) -> str:  # 启动
        return "run-1"  # 固定 id

    async def events(self, run_id: str, *, last_event_id: str = "") -> Any:  # 事件流
        del run_id, last_event_id  # 不用
        for ev in self.items:  # 逐条
            yield ev  # 产出裸事件


class _BoomClient:  # 订阅时抛非网络异常的客户端
    async def start(self, _run_input: Any) -> str:  # 启动
        return "run-boom"  # 固定 id

    async def events(self, run_id: str, *, last_event_id: str = "") -> Any:  # 事件流
        del run_id, last_event_id  # 不用
        raise RuntimeError("boom")  # 模拟下游实现抛错（不是 HTTP/网络错误）
        yield  # 让函数成为异步生成器（永不执行）


def _run(coro: Any) -> Any:  # 同步跑
    return asyncio.run(coro)  # 完成


def test_session_feeds_events_then_sentinel() -> None:  # 入队 + 结束
    items = [agent_events.ack("x"), agent_events.ack("x")]  # 两个事件
    client = FakeClient(items)  # 假客户端

    async def main() -> tuple[int, list[Any]]:  # 异步主体
        session = await RunSession.create(client, None)  # 启动会话
        got: list[Any] = []  # 收集
        count = 0  # sentinel 前数量
        while True:  # 消费
            event = await session.events.get()  # 取
            if event is SENTINEL:  # 结束
                break  # 停
            got.append(event)  # 追加
            count += 1  # 计数
        return count, got  # 返回

    count, got = _run(main())  # 跑
    assert count == 2  # 两个事件
    assert got == items  # 顺序一致


def test_session_feeds_bare_events_then_sentinel() -> None:  # 裸事件入队 + 结束
    """dev 模式无 worker 时 get_run_client 回退 InProcessClient，其 events 产出裸 Event。

    回归：_feed 若按 (seq, event) 硬解包会 TypeError 崩溃、不放 SENTINEL、UI 卡到超时。
    """
    items = [agent_events.ack("x"), agent_events.ack("y")]  # 两个裸事件
    client = BareClient(items)  # 裸事件客户端

    async def main() -> tuple[int, list[Any]]:  # 异步主体
        session = await RunSession.create(client, None)  # 启动会话
        got: list[Any] = []  # 收集
        count = 0  # sentinel 前数量
        while True:  # 消费
            event = await session.events.get()  # 取
            if event is SENTINEL:  # 结束
                break  # 停
            got.append(event)  # 追加
            count += 1  # 计数
        return count, got  # 返回

    count, got = _run(main())  # 跑
    assert count == 2  # 两个事件
    assert got == items  # 顺序一致


def test_feed_puts_sentinel_even_on_unexpected_error() -> None:  # 异常也要收尾
    """订阅过程中抛非网络异常时，``_feed`` 仍必须放 SENTINEL。

    回归：以前只有"正常结束/HTTP 错误"才放结束标记，别的异常会让后台任务静默死掉，
    UI 永远等不到结束 → 只能等 600s 兜底超时。这里用 wait_for 兜时间：坏代码会超时失败。
    """

    async def main() -> Any:  # 异步主体
        session = await RunSession.create(_BoomClient(), None)  # 启动
        return await asyncio.wait_for(session.events.get(), timeout=3.0)  # 必须在 3s 内拿到

    assert _run(main()) is SENTINEL  # 拿到结束标记


def test_session_cancel_and_approve_forward() -> None:  # 控制转发
    client = FakeClient([])  # 空流
    cancelled: list[str] = []  # 记录
    approved: list[tuple[str, str, bool]] = []

    async def main() -> None:  # 异步主体
        session = await RunSession.create(client, None)  # 启动
        session.cancel()  # 取消（同步）
        session.approve("req1", True)  # 审批（同步）
        await asyncio.sleep(0)  # 让后台任务落地
        cancelled.extend(client.cancelled)  # 汇总
        approved.extend(client.approved)  # 汇总

    _run(main())  # 跑
    assert cancelled == ["run-1"]  # 转发到
    assert approved == [("run-1", "req1", True)]  # 审批转发
