"""MCP 连接生命周期：复用/缓存/空闲回收/优雅关闭。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 异步
from typing import ClassVar  # 类变量

from pydantic import BaseModel  # 契约

from app.agent.skills import mcp  # 消费模块
from app.agent.skills.mcp_manager import McpManager  # 管理器


class _Args(BaseModel):  # 假参数
    text: str = "x"  # 字段


class _Remote:  # 假远端工具
    name = "echo"  # 名
    description = "echo"  # 描述
    args_schema = _Args  # 契约

    async def ainvoke(self, args):  # 调用
        del args  # 不用
        return "pong"  # 结果


class _FakeClient:  # 假客户端（记录生命周期）
    instances: ClassVar[list] = []  # 实例表

    def __init__(self, spec):  # 构造
        del spec  # 不用
        self.entered = False  # 进入
        self.exited = False  # 退出
        type(self).instances.append(self)  # 记录

    async def __aenter__(self):  # 进入
        self.entered = True  # 标记
        return self  # 返回

    async def __aexit__(self, *a):  # 退出
        del a  # 不用
        self.exited = True  # 标记

    async def get_tools(self):  # 取工具
        return [_Remote()]  # 一个


def test_reuse_and_cache(monkeypatch) -> None:  # 连接复用 + 工具缓存
    _FakeClient.instances.clear()  # 清
    monkeypatch.setattr(mcp, "_make_client", lambda spec: _FakeClient(spec))  # 打桩
    mgr = McpManager()  # 管理器
    spec = {"command": "python", "args": ["s.py"], "transport": "stdio"}  # 规格
    t1 = asyncio.run(mgr.get_tools("s", spec))  # 第一次
    t2 = asyncio.run(mgr.get_tools("s", spec))  # 第二次
    assert len(t1) == 1 and t1[0].name == "echo"  # 工具
    assert len(_FakeClient.instances) == 1  # 只建一次连接
    assert mgr.open_servers() == ["s"]  # 打开
    assert t2[0].name == "echo"  # 缓存可用


def test_close_all(monkeypatch) -> None:  # 优雅关闭
    _FakeClient.instances.clear()  # 清
    monkeypatch.setattr(mcp, "_make_client", lambda spec: _FakeClient(spec))  # 打桩
    mgr = McpManager()  # 管理器
    asyncio.run(mgr.get_tools("s", {"command": "x"}))  # 打开
    asyncio.run(mgr.close_all())  # 关
    assert mgr.open_servers() == []  # 全关
    assert _FakeClient.instances[0].exited is True  # 已退出


def test_evict_idle(monkeypatch) -> None:  # 空闲回收
    _FakeClient.instances.clear()  # 清
    monkeypatch.setattr(mcp, "_make_client", lambda spec: _FakeClient(spec))  # 打桩
    mgr = McpManager()  # 管理器
    asyncio.run(mgr.get_tools("s", {"command": "x"}))  # 打开
    evicted = asyncio.run(mgr.evict_idle(0.0, now=__import__("time").monotonic() + 100))  # 全空闲（用大 now）
    assert evicted == ["s"]  # 回收了
    assert mgr.open_servers() == []  # 已清
