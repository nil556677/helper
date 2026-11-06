"""HITL resume 的 RPC 链路：LocalBackend 转发 + RunSession 发送。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 跑任务
from typing import Any  # 任意值

from app.agent.run.local import LocalBackend  # 后端
from app.agent.run.session import RunSession  # 会话
from app.agent.run.types import RunInput  # 入参


class _Handle:  # 假流句柄
    def __init__(self) -> None:  # 构造
        self.run_id = "r1"  # id
        self.events: asyncio.Queue = asyncio.Queue()  # 队列
        self.resumed: list[Any] = []  # 记录恢复值

    async def resume(self, value: Any) -> None:  # 恢复
        self.resumed.append(value)  # 记录


async def test_local_backend_forwards_resume() -> None:  # 后端转发
    handle = _Handle()  # 句柄
    backend = LocalBackend(factory=lambda *a, **k: handle)  # 工厂
    run_id = await backend.start(RunInput(question="q", conversation_id="c"))  # 启动
    await backend.resume(run_id, "time")  # 恢复
    assert handle.resumed == ["time"]  # 转发到句柄


class _Client:  # 假网关
    def __init__(self) -> None:  # 构造
        self.calls: list[tuple] = []  # 记录

    async def resume(self, run_id: str, value: Any) -> None:  # 恢复
        self.calls.append((run_id, value))  # 记录


async def test_session_spawns_resume() -> None:  # 会话发送
    client = _Client()  # 假网关
    session = RunSession(client, "r1")  # 会话
    session.resume("time")  # 恢复（起后台任务）
    await asyncio.sleep(0)  # 让任务跑起来
    await asyncio.sleep(0)  # 再让一步
    assert client.calls == [("r1", "time")]  # 已发送