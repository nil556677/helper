"""trace 异步写入：Span 收尾等写操作入队，后台批量落库，不阻塞 token 流。

- 有事件循环：入队 + 后台 `to_thread` 批量写；
- 没有事件循环（单测/批处理）：退化为同步执行，语义不变；
- 队列满：丢弃并告警（trace 永远不阻断业务）。
"""

from __future__ import annotations

import asyncio  # 队列/后台任务
from collections.abc import Callable  # 写操作类型

from loguru import logger  # 日志

__all__ = ["flush", "submit"]  # 对外导出

_MAX_QUEUE = 4096  # 队列上限
_BATCH = 64  # 每批最多写多少条

_LOOP: asyncio.AbstractEventLoop | None = None  # 所属事件循环
_QUEUE: asyncio.Queue[Callable[[], None]] | None = None  # 待写队列
_WORKER: asyncio.Task | None = None  # 后台任务


def _safe(fn: Callable[[], None]) -> None:  # 执行一次写
    """写失败只记日志。"""
    try:  # 兜底
        fn()  # 执行
    except Exception as exc:  # 失败
        logger.opt(exception=exc).warning("trace 写入失败")  # 告警


def _drain_batch(batch: list[Callable[[], None]]) -> None:  # 批量写
    """在后台线程里逐条执行（SQLite 写不占事件循环）。"""
    for fn in batch:  # 逐条
        _safe(fn)  # 执行


async def _drain(queue: asyncio.Queue[Callable[[], None]]) -> None:  # 后台消费
    """取一批、丢线程池写、标记完成。"""
    while True:  # 常驻
        first = await queue.get()  # 阻塞取一条
        batch = [first]  # 本批
        while len(batch) < _BATCH:  # 尽量凑批
            try:  # 非阻塞取
                batch.append(queue.get_nowait())  # 取
            except asyncio.QueueEmpty:  # 没了
                break  # 收手
        try:  # 写
            await asyncio.to_thread(_drain_batch, batch)  # 线程池执行
        finally:  # 一定要标记
            for _ in batch:  # 逐条
                queue.task_done()  # 完成


def submit(fn: Callable[[], None]) -> None:  # 提交写操作
    """有事件循环就入队；没有就同步执行。"""
    global _LOOP, _QUEUE, _WORKER  # 模块级
    try:  # 取当前循环
        loop = asyncio.get_running_loop()  # 运行中
    except RuntimeError:  # 没循环
        _safe(fn)  # 同步写
        return  # 结束
    if _LOOP is not loop:  # 换了循环（测试常见）
        _LOOP = loop  # 记
        _QUEUE = asyncio.Queue(maxsize=_MAX_QUEUE)  # 新队列
        _WORKER = loop.create_task(_drain(_QUEUE))  # 新后台任务
    assert _QUEUE is not None  # 类型收窄
    try:  # 入队
        _QUEUE.put_nowait(fn)  # 非阻塞
    except asyncio.QueueFull:  # 满了
        logger.warning("trace 写入队列已满，丢弃一条")  # 丢


async def flush(timeout: float = 5.0) -> None:  # 排空
    """等队列写完（收尾用；超时只告警）。"""
    queue = _QUEUE  # 当前队列
    if queue is None:  # 没队列
        return  # 结束
    try:  # 等
        await asyncio.wait_for(queue.join(), timeout)  # 全部完成
    except TimeoutError:  # 超时
        logger.warning("trace flush 超时（{}s）", timeout)  # 告警
