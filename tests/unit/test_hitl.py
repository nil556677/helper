"""HITL 原语：待审批队列能注册、释放、幂等清理。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 事件循环

from app.agent.hitl import ApprovalStore, make_approver  # 被测


async def test_approver_waits_and_follows_resolve() -> None:  # 审批释放驱动
    approver = make_approver("r1")  # 审批器

    async def ask() -> bool:  # 发起一个审批请求
        return await approver("general_agent", {"step": "delete"})  # 不可逆步

    task = asyncio.create_task(ask())  # 后台等
    await asyncio.sleep(0)  # 让它注册
    assert ApprovalStore.pending_count() == 1  # 有一个待审批
    ok = ApprovalStore.resolve("r1", "approve-r1-1", allow=True)  # 用户批准
    assert ok is True  # 释放成功
    assert await task is True  # run 侧得到 True


async def test_resolve_denied() -> None:  # 拒绝 → run 得到 False
    approver = make_approver("r2")  # 审批器

    async def ask() -> bool:  # 请求
        return await approver("general_agent", {"step": "delete"})  # 不可逆

    task = asyncio.create_task(ask())  # 后台
    await asyncio.sleep(0)  # 注册
    ApprovalStore.resolve("r2", "approve-r2-1", allow=False)  # 拒绝
    assert await task is False  # run 侧 False


async def test_resolve_unknown_is_noop() -> None:  # 未知/重复幂等
    assert ApprovalStore.resolve("x", "nope", True) is False  # 不存在 → False
    ApprovalStore.resolve("y", "a", True)  # 空操作（没有先注册也无妨）