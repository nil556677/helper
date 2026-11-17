"""MCP Consumer P1：可用性 / 元数据翻译(dsh) / 注册 / 门控 / 装载(假适配器)。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 异步
from types import SimpleNamespace  # 假对象

import pytest  # 断言
from pydantic import BaseModel  # 参数契约

from app.agent.skills import mcp  # 被测
from app.agent.skills import registry as skills_registry  # 注册表
from app.agent.skills.mcp import (  # 导入
    McpServerConfig,
    McpUnavailable,
    adopt,
    gated,
    load_tools,
    register,
)


class _AnyArgs(BaseModel):  # 假参数契约
    text: str = "x"  # 字段


class _FakeRemote:  # 假远端工具
    name = "echo"  # 名
    description = "echo server"  # 描述
    args_schema = _AnyArgs  # 契约

    async def ainvoke(self, kwargs):  # 调用
        del kwargs  # 不用
        return "pong"  # 结果


def test_available_true() -> None:  # 依赖已装且客户端可导入 → 可用（环境版本不符则跳过）
    if not mcp.available():  # 本机 mcp/adapter 版本不兼容
        pytest.skip("MCP deps 不可用/版本不兼容")  # 跳过
    assert mcp.available() is True  # 可用


def test_gated_off_by_default() -> None:  # 门控默认关
    assert gated("weather") is False  # 关


def test_gated_on_by_env(monkeypatch) -> None:  # 白名单开
    monkeypatch.setenv("HELPER_MCP_ENABLED", "weather,tools")  # 白名单
    assert gated("weather") is True  # 开
    assert gated("other") is False  # 未白名单


def test_adopt_carries_dsh_metadata() -> None:  # adopt 补 dsh，审批/审计可见
    cfg = McpServerConfig(server_id="s", mutation="read_only", approval="none", risk="low")  # 配置
    tool = adopt(_FakeRemote(), config=cfg)  # 包装
    assert tool.name == "echo"  # 名
    meta = (tool.metadata or {}).get("dsh", {})  # dsh
    assert meta.get("approval") == "none"  # 审批
    assert meta.get("mutation") == "read_only"  # 变更
    result = asyncio.run(tool.ainvoke({"text": "hi"}))  # 调用
    assert result == "pong"  # 转发成功


def test_register_into_extra(monkeypatch) -> None:  # 注册进 EXTRA_TOOLS（MCP 缝）
    fake_tool = SimpleNamespace(name="mcp_echo", _t=object())  # 假工具
    reg = register([fake_tool])  # 注册（写进同一 EXTRA_TOOLS dict）
    try:  # 清理
        assert reg.get("mcp_echo") is fake_tool  # 已注册
        assert isinstance(skills_registry.EXTRA_TOOLS, dict)  # 可写
    finally:  # 还原
        skills_registry.EXTRA_TOOLS.pop("mcp_echo", None)  # 移除


def test_load_tools_degrades_when_unavailable(monkeypatch) -> None:  # 缺依赖优雅降级
    monkeypatch.setattr(mcp, "available", lambda: False)  # 假装不可用
    with pytest.raises(McpUnavailable):  # 抛错
        asyncio.run(load_tools(McpServerConfig(server_id="s", command="python", args=["server.py"])))  # 装载


def test_load_tools_via_adapter(monkeypatch) -> None:  # 经适配器装载 + 翻译
    monkeypatch.setattr(mcp, "available", lambda: True)  # 假装可用

    class _FakeClient:  # 假客户端
        def __init__(self, spec):  # 构造
            del spec  # 不用
            self._tools = [_FakeRemote()]  # 一个远端

        async def __aenter__(self):  # 进入
            return self  # 返回

        async def __aexit__(self, *a):  # 退出
            del a  # 不用

        async def get_tools(self):  # 取工具
            return list(self._tools)  # 返回

    monkeypatch.setattr(mcp, "_make_client", lambda spec: _FakeClient(spec))  # 打桩客户端工厂
    tools = asyncio.run(load_tools(McpServerConfig(server_id="s", command="python", args=["s.py"])))  # 装载
    assert len(tools) == 1 and tools[0].name == "echo"  # 一个翻译后的工具
    assert (tools[0].metadata or {}).get("dsh")  # 带 dsh
