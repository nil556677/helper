"""HITL 原语：进程内待审批队列 (run_id, request_id) → asyncio.Future。

供应一座桥：`run_fallback` 的审批接缝（`approve(agent, step)`）请求一个 `Future`，
HTTP 审批接口拿到结论后用 `resolve` **释放**它——run 侧 await 到结论继续/拒绝。
"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # Future
import itertools  # 计数
import threading  # 锁
from typing import Awaitable, Callable  # 类型

from app.agent.fallback.run import Approver  # 审批签名


class ApprovalStore:  # 进程级待审批队列
    """按 (run_id, request_id) 存 Future；register 在 run loop，resolve 在 HTTP loop。"""

    _lock = threading.Lock()  # 跨线程锁
    _pending: dict[tuple[str, str], asyncio.Future] = {}  # 待审批

    @classmethod
    def register(cls, run_id: str, request_id: str) -> asyncio.Future:  # 注册一个待审批
        fut: asyncio.Future = asyncio.get_running_loop().create_future()  # 建 Future
        with cls._lock:  # 加锁
            cls._pending[(run_id, request_id)] = fut  # 登记
        return fut  # 返回（run 侧 await 它）

    @classmethod
    def resolve(cls, run_id: str, request_id: str, allow: bool) -> bool:  # 释放结论
        with cls._lock:  # 加锁
            fut = cls._pending.pop((run_id, request_id), None)  # 取出
        if fut is None:  # 不存在/已处理
            return False  # 幂等拒绝
        if not fut.done():  # 未完成
            fut.set_result(allow)  # 给结论
        return True  # 已释放

    @classmethod
    def pending_count(cls) -> int:  # 待审批数
        with cls._lock:  # 加锁
            return len(cls._pending)  # 数量


def make_approver(run_id: str) -> Approver:  # 把审批接缝接到待审批队列
    """返回 `(agent, step) -> bool`；每个请求注册一个待审批并等用户结论。"""
    counter = itertools.count(1)  # 请求自增

    async def approve(agent: str, step: dict) -> bool:  # 审批
        request_id = f"approve-{run_id}-{next(counter)}"  # 请求 id
        fut: asyncio.Future = ApprovalStore.register(run_id, request_id)  # 注册
        return bool(await fut)  # 等结论（await 被 resolve 释放）

    return approve  # 返回