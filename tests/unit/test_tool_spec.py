"""tool_spec 统一范式单测：错误分流 / 缓存命中 / 元数据 / 同步异步。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 异步测试

from pydantic import BaseModel, Field  # 参数契约

from app.agent.contracts import ErrorCode, Mutation  # 错误码 / 变更画像
from app.agent.tools import wiring  # 接线（describe）
from app.agent.tools.tooling import ToolError, tool_spec  # 统一范式

_calls: list = []  # 调用计数（测缓存）


class P(BaseModel):  # 参数契约
    q: str = Field(description="q")  # 必填
    n: int = Field(default=1, description="n")  # 默认


@tool_spec(description="正常工具", args_schema=P, ttl=3600)
def good(q: str, n: int = 1):  # 纯函数
    _calls.append((q, n))  # 计数
    return {"q": q, "n": n}  # data


@tool_spec(description="业务错工具", args_schema=P)
def bizfail(q: str, n: int = 1):  # 纯函数
    raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), f"参数错：{q}")  # 业务错


@tool_spec(description="上游错工具", args_schema=P)
def upstream(q: str, n: int = 1):  # 纯函数
    raise TimeoutError("模型超时")  # 上游瞬时


@tool_spec(description="bug 工具", args_schema=P)
def buggy(q: str, n: int = 1):  # 纯函数
    raise KeyError("missing")  # 编程 Bug


@tool_spec(description="异步工具", args_schema=P)
async def async_tool(q: str, n: int = 1):  # 异步纯函数
    return {"q": q, "n": n}  # data


@tool_spec(description="带元数据", args_schema=P, version=3, permissions=("kb",))
def meta_tool(q: str, n: int = 1):  # 纯函数
    return {"q": q}  # data


def test_sync_success() -> None:  # 成功
    env = good.invoke({"q": "x"})  # 同步调
    assert env.ok  # 成功
    assert env.data == {"q": "x", "n": 1}  # 含默认值


def test_tool_error_is_protocol() -> None:  # 业务错
    env = bizfail.invoke({"q": "x"})  # 调
    assert not env.ok  # 失败
    assert env.code == ErrorCode.PROTOCOL_ARG_INVALID  # 业务码
    assert env.retryable is False  # 业务错不重试


def test_upstream_retryable() -> None:  # 上游瞬时
    env = upstream.invoke({"q": "x"})  # 调
    assert env.code == ErrorCode.EXECUTION_FAILED  # 执行层
    assert env.retryable is True  # 可重试


def test_bug_not_retryable() -> None:  # 编程 Bug
    env = buggy.invoke({"q": "x"})  # 调
    assert env.code == ErrorCode.INTERNAL_ERROR  # 内部错误码
    assert env.retryable is False  # 不让重试掩盖 Bug


def test_cache_single_run_and_marked() -> None:  # 缓存命中一次 + 标记
    _calls.clear()  # 清零
    good.invoke({"q": "a"})  # 第一次
    good.invoke({"q": "a"})  # 第二次
    second = good.invoke({"q": "a"})  # 第三次
    assert len(_calls) == 1  # 只真跑一次
    assert second.meta.get("cached") is True  # 命中可辨识


def test_describe_reads_metadata() -> None:  # 元数据随工具走
    desc = wiring.describe(meta_tool)  # 读元数据
    assert desc.version == 3  # 版本
    assert desc.permissions == ("kb",)  # 权限
    assert desc.mutation is Mutation.READ_ONLY  # 变更画像默认


def test_async_tool() -> None:  # 异步工具
    env = asyncio.run(async_tool.ainvoke({"q": "x", "n": 2}))  # 异步调
    assert env.ok  # 成功
    assert env.data == {"q": "x", "n": 2}  # data


def test_async_single_flight_not_crash() -> None:  # 并发同参不崩
    async def run():  # 并发两发
        return await asyncio.gather(  # gather
            async_tool.ainvoke({"q": "s"}),  # 第一发
            async_tool.ainvoke({"q": "s"}),  # 第二发（合并）
        )  # 结束
    a, b = asyncio.run(run())  # 跑
    assert a.ok and b.ok  # 都成功