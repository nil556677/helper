"""事件缓冲与后端门面（从旧的 run_server 抽出）。

- `Feed`：单 run 的事件缓冲（seq 单调 + deque + wake），供 SSE 按 ``Last-Event-ID`` 续传；
- `ServerBackend`：把任意 ``RunGateway`` 后端包装成「建 feed + 转发命令」的门面。
"""
from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 队列/事件
from collections import deque  # 有界缓冲
from typing import Any  # 任意值

from app.agent.run.gateway import RunGateway  # 网关协议
from app.agent.run.types import RunInput  # 入参

#: 每 run 事件缓冲上限（够断线续传；history 回看走 DB）。
_BUFFER_CAP = 4000
#: SSE 心跳间隔（秒）：防代理/空闲断开。
_HEARTBEAT_S = 15.0
#: 结束后的 feed 保留时长（秒）：留断线重连窗口，之后回收。
_FEED_TTL_S = 600.0


class Feed:  # 单 run 的事件缓冲 + 后台消费
    """一个 run 只消费一次 events()，全部进缓冲；SSE 从缓冲读。"""

    def __init__(self, backend: RunGateway, run_id: str) -> None:  # 构造
        self.backend = backend  # 后端
        self.run_id = run_id  # run id
        self.seq = 0  # 单调序号
        self.buf: deque[tuple[int, Any]] = deque(maxlen=_BUFFER_CAP)  # (seq, event)
        self.done = False  # 是否消费完
        self.wake: asyncio.Event = asyncio.Event()  # 有新数据/结束唤醒
        self._task: asyncio.Task | None = None  # 后台任务

    async def run(self) -> None:  # 消费循环
        """消费后端事件流；结束置 done 并唤醒订阅者。"""
        try:  # 逐条
            async for event in self.backend.events(self.run_id):  # 后端流
                self.seq += 1  # 递增
                self.buf.append((self.seq, event))  # 入缓冲
                self.wake.set()  # 唤醒
        finally:  # 结束
            self.done = True  # 标记完成
            self.wake.set()  # 再唤醒

    def emit_pending(self, after: int) -> list[tuple[int, Any]]:  # 取某序号之后
        """返回 ``(seq, event)`` 中 > after 的部分（已排序）。"""
        return [(seq, e) for seq, e in self.buf if seq > after]  # 过滤

    async def wait_cleared(self, after: int) -> None:  # 等下一条可发的
        """先清 wake 再等新数据；无超时（心跳由 sse-starlette 负责）。

        **必须先清 wake 再等**：它是「有新数据」的电平信号，不清就一直为真，
        ``wait()`` 立即返回 → 不挂起死循环（sse-starlette 的 ping 任务会占住 loop）。
        """
        self.wake.clear()  # 清旧唤醒
        if self.buf and self.buf[-1][0] > after:  # 清完又有新数据（并发写入）
            return  # 别等，马上发
        await self.wake.wait()  # 等新数据或结束


class ServerBackend:  # 服务端门面
    """命令转发 + feed 管理。"""

    def __init__(self, backend: RunGateway) -> None:  # 构造
        self.backend = backend  # 真正的后端
        self.feeds: dict[str, Feed] = {}  # run_id → feed

    async def create(self, run_input: RunInput) -> str:  # 启一轮
        run_id = await self.backend.start(run_input)  # 启动
        feed = Feed(self.backend, run_id)  # 建 feed
        self.feeds[run_id] = feed  # 记录
        loop = asyncio.get_running_loop()  # 当前 loop
        feed._task = loop.create_task(feed.run())  # 后台消费
        # 必须有回收：feed 带着整轮事件缓冲，只进不出会随轮次涨内存。
        loop.call_later(_FEED_TTL_S, self._evict, run_id, feed)  # 定时回收
        return run_id  # run id

    def feed(self, run_id: str) -> Feed | None:  # 取 feed
        return self.feeds.get(run_id)  # 查

    def _evict(self, run_id: str, feed: Feed) -> None:  # 回收一个 feed
        """已结束且过了保留期就摘掉；还在跑就顺延。"""
        if self.feeds.get(run_id) is not feed:  # 已被替换/回收
            return  # 结束
        if not feed.done:  # 还在跑
            asyncio.get_running_loop().call_later(_FEED_TTL_S, self._evict, run_id, feed)  # 顺延
            return  # 结束
        self.feeds.pop(run_id, None)  # 摘掉
