"""事件缓冲与后端门面（从旧的 run_server 抽出）。

- `Feed`：单 run 的事件缓冲（seq 单调 + list/head 指针 + wake），供 SSE 按 ``Last-Event-ID`` 续传；
- `ServerBackend`：把任意 ``RunGateway`` 后端包装成「建 feed + 转发命令」的门面。
"""
from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 队列/事件
from typing import Any  # 任意值

from app.agent.run.gateway import RunGateway  # 网关协议
from app.agent.run.types import RunInput  # 入参

#: 每 run 事件缓冲上限（够断线续传；history 回看走 DB）。
_BUFFER_CAP = 4000
#: 结束后的 feed 保留时长（秒）：留断线重连窗口，之后回收。
_FEED_TTL_S = 600.0


class Feed:  # 单 run 的事件缓冲 + 后台消费
    """一个 run 只消费一次 events()，全部进缓冲；SSE 从缓冲读。"""

    def __init__(self, backend: RunGateway, run_id: str) -> None:  # 构造
        self.backend = backend  # 后端
        self.run_id = run_id  # run id
        self.seq = 0  # 单调序号（每次 append 恒 +1，缓冲内 seq 连续整数）
        self.buf: list[tuple[int, Any]] = []  # (seq, event)，按 seq 升序
        self.head = 0  # buf[head:] 才是有效内容（挤头只挪指针，不搬数据）
        self.done = False  # 是否消费完
        self.wake: asyncio.Event = asyncio.Event()  # 有新数据/结束唤醒
        self._task: asyncio.Task | None = None  # 后台任务

    async def run(self) -> None:  # 消费循环
        """消费后端事件流；结束置 done 并唤醒订阅者。"""
        try:  # 逐条
            async for event in self.backend.events(self.run_id):  # 后端流
                self.seq += 1  # 递增
                self.buf.append((self.seq, event))  # 入缓冲
                self._trim()  # 只保留最近 _BUFFER_CAP 条（防超长 run 涨内存）
                self.wake.set()  # 唤醒
        finally:  # 结束
            self.done = True  # 标记完成
            self.wake.set()  # 再唤醒

    def _trim(self) -> None:  # 挤掉旧头，必要时物理压缩
        """只保留最近 ``_BUFFER_CAP`` 条：先挪 head 指针，指针攒大后整体截断一次。"""
        drop = len(self.buf) - self.head - _BUFFER_CAP  # 超出上限的条数
        if drop > 0:  # 超了
            self.head += drop  # 只挪指针（旧数据标记作废，不移元素）
        if self.head >= _BUFFER_CAP:  # 指针攒太多，物理长度失控
            self.buf = self.buf[self.head:]  # 整体截断，回收前面已作废的内存
            self.head = 0  # 指针归零

    def emit_pending(self, after: int) -> list[tuple[int, Any]]:  # 取某序号之后
        """返回 ``(seq, event)`` 中 > after 的部分（按 seq 升序，已排序）。

        因 ``seq`` 每次 append 恒 +1，缓冲内的 seq 是连续整数，故首个 > after 的下标可由
        ``after - 最低 seq`` 直接算出，只切需要发的那段尾部，省掉从头整扫。
        """
        if not self.buf or self.head >= len(self.buf):  # 空缓冲
            return []  # 无内容
        low = self.buf[self.head][0]  # 当前有效首条的 seq
        if after < low:  # 客户端落后于缓冲最老条（已挤掉过）→ 从有效起点给
            start = self.head  # 全部从有效头开始
        else:  # 正常：越过已发过的部分
            start = self.head + (after - low) + 1  # 首个 > after 的下标（O(1) 算出）
        return self.buf[start:]  # 只切需要发的那段尾部

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
