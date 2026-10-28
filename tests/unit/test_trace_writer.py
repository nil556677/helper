"""trace 异步写入：入队/批量/同步回退（类 D · D3）。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 跑异步

from app.core.trace import writer  # 被测模块


def test_submit_falls_back_to_sync_without_loop() -> None:  # 无循环
    """没有事件循环时同步执行（单测/批处理语义不变）。"""
    calls: list[int] = []  # 记录
    writer.submit(lambda: calls.append(1))  # 提交
    assert calls == [1]  # 立即执行


def test_submit_async_flush() -> None:  # 有循环
    """有循环时入队，flush 后排空。"""
    async def main() -> list[int]:  # 异步壳
        calls: list[int] = []  # 记录
        writer.submit(lambda: calls.append(1))  # 提交
        writer.submit(lambda: calls.append(2))  # 提交
        await writer.flush()  # 排空
        return calls  # 返回

    assert asyncio.run(main()) == [1, 2]  # 顺序执行


def test_submit_swallows_errors() -> None:  # 写失败不抛
    """写操作抛错只记日志。"""
    def boom() -> None:  # 炸
        raise RuntimeError("db down")  # 抛

    writer.submit(boom)  # 不应抛
