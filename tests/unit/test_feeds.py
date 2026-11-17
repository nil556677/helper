"""Feed 缓冲单测：验证 list/head 裁剪后的续传定位与内存上限（#2 优化）。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 跑消费循环
from typing import Any  # 任意值

from app.http.feeds import _BUFFER_CAP, Feed  # 缓冲实现与上限


class _FakeBackend:  # 假后端
    """只产 N 条递增事件；喂给 Feed.run 触发裁剪。"""

    def __init__(self, count: int) -> None:  # 构造
        self.count = count  # 事件总数

    async def events(self, run_id: str) -> Any:  # 事件流
        del run_id  # 不用
        for i in range(1, self.count + 1):  # 1..N
            yield i  # 事件体（这里用 int 代替事件对象）


async def _run_feed(count: int) -> Feed:  # 跑完一个 feed
    """消费 N 条事件到 done，返回 feed 供断言。"""
    feed = Feed(_FakeBackend(count), "r1")  # 建 feed
    await feed.run()  # 消费到结束
    return feed  # 返回


async def test_emit_pending_positions() -> None:  # 各位置定位
    """after 在头/中/尾/超尾时的切片结果正确。"""
    feed = await _run_feed(10)  # 10 条，seq 1..10，未超上限
    assert feed.emit_pending(0) == [(s, s) for s in range(1, 11)]  # 从头全给
    assert [s for s, _ in feed.emit_pending(5)] == [6, 7, 8, 9, 10]  # 越过 5
    assert feed.emit_pending(10) == []  # 已到最新
    assert feed.emit_pending(99) == []  # 超尾


async def test_trim_keeps_cap_and_contiguous() -> None:  # 裁剪保上限且连续
    """超上限后只留最近 _BUFFER_CAP 条，seq 仍连续、内存物理长度有界。"""
    total = _BUFFER_CAP + 5  # 10005 条
    feed = await _run_feed(total)  # 触发多次裁剪
    retained = len(feed.buf) - feed.head  # 有效条数
    assert retained == _BUFFER_CAP  # 只留最近 4000 条
    low = feed.buf[feed.head][0]  # 有效首条 seq
    assert low == total - _BUFFER_CAP + 1  # = 6006
    assert feed.buf[-1][0] == total  # 尾部是最后一条
    # 物理长度有界：指针攒够才截断，最多 ~上限截断阈值内。
    assert len(feed.buf) <= _BUFFER_CAP * 2  # 物理长度有上限
    # 从有效头读出的 seq 是连续整数（>after 定位的数学前提）。
    seqs = [s for s, _ in feed.emit_pending(low - 1)]  # 从有效头起读
    assert seqs == list(range(low, total + 1))  # 连续无缺
    assert feed.emit_pending(total) == []  # 读尽


async def test_after_older_than_trimmed_head() -> None:  # 落后于被挤老头
    """客户端续传点比被挤出缓冲的最老条还旧 → 从有效头全给，不回退到已丢事件。"""
    total = _BUFFER_CAP + 5  # 触发裁剪
    feed = await _run_feed(total)  # 跑完
    low = feed.buf[feed.head][0]  # 有效最小 seq（被挤出前的旧 seq 已不可读）
    ahead = [s for s, _ in feed.emit_pending(1)]  # 续传点 1（比有效头还旧）
    assert ahead == [s for s, _ in feed.emit_pending(low - 1)]  # 与"从有效头全给"等价
    assert ahead[0] == low  # 起点就是有效首条
    assert len(ahead) == _BUFFER_CAP  # 可给的就是保留的整段