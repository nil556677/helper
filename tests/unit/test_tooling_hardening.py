"""工具基础层加固：LRU / 深拷贝隔离 / result_schema / timeout_ms / render 只给 data / collect __all__。"""

from __future__ import annotations  # 延迟值注解

import json  # 解析 render
from types import SimpleNamespace  # 假模块

from pydantic import BaseModel  # 结果契约

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.tools.cache import TtlCache  # 缓存
from app.agent.tools.tooling import tool_spec  # 装饰器
from app.agent.tools.wiring import collect, describe, render, unwrap_tool_output  # 接线
from app.core.error import err as make_err  # 失败包络
from app.core.error import ok as make_ok  # 成功包络


def test_lru_evicts_least_recently_used() -> None:  # LRU：最近访问的保留
    cache = TtlCache(ttl=60, max_entries=2)  # 容量 2
    cache.set("a", 1)  # 写 a
    cache.set("b", 2)  # 写 b
    cache.get("a")  # 访问 a → a 变成最近使用
    cache.set("c", 3)  # 写 c → 超容量淘汰 b（最久未用）
    assert cache.get("a") == 1  # a 还在
    assert cache.get("b") is None  # b 被淘汰
    assert cache.get("c") == 3  # c 在


def test_set_copies_value() -> None:  # 写入即拷贝，外部改动不污染缓存
    cache = TtlCache(ttl=60)  # 无容量上限
    payload = {"items": [1]}  # 可变对象
    cache.set("k", payload)  # 写
    payload["items"].append(2)  # 改外部
    assert cache.get("k") == {"items": [1]}  # 缓存不受影响


def test_get_returns_copy() -> None:  # 读出深拷贝，调用方改元素不污染
    cache = TtlCache(ttl=60)  # 缓存
    cache.set("k", {"items": [1]})  # 写
    first = cache.get("k")  # 第一次取
    first["items"].append(9)  # 改返回值
    assert cache.get("k") == {"items": [1]}  # 缓存仍原样


class _Data(BaseModel):  # 结果契约
    q: str = ""  # 字段


class _Args(BaseModel):  # 参数契约
    q: str = ""  # 字段


@tool_spec(description="带结果契约", args_schema=_Args, result_schema=_Data, timeout_ms=8500)  # 声明
def _declared(q: str = ""):  # 纯函数
    return {"q": q}  # data


def test_describe_passes_result_schema_and_timeout() -> None:  # describe 透传 result_schema/timeout_ms
    desc = describe(_declared)  # 读元数据
    assert desc.result_schema is _Data  # 结果契约
    assert desc.timeout_ms == 8500  # 超时


def test_render_success_only_data() -> None:  # 模型只看到 data
    text = render(make_ok({"a": 1}))  # 渲染成功
    assert json.loads(text) == {"a": 1}  # 只有 data


def test_render_error_minimal() -> None:  # 错误只给 code/message/retryable
    text = render(make_err(str(ErrorCode.PROTOCOL_ARG_INVALID), "坏参数", retryable=False))  # 渲染失败
    obj = json.loads(text)  # 解析
    assert set(obj) == {"code", "message", "retryable"}  # 无框架字段


def test_unwrap_success_and_error() -> None:  # 程序化还原
    good = unwrap_tool_output(json.dumps({"a": 1}))  # 成功 data JSON
    assert good.ok and good.data == {"a": 1}  # 还原成功
    bad = unwrap_tool_output(json.dumps({"code": "x", "message": "m", "retryable": True}))  # 错误 JSON
    assert bad.ok is False and bad.code == "x" and bad.retryable is True  # 还原失败


def test_collect_prefers_all() -> None:  # __all__ 过滤，误收的同模块工具不入
    from langchain_core.tools import tool as make_tool  # 官方 @tool

    @make_tool  # 声明为工具
    def keep() -> str:  # 该收
        """保留工具。"""
        return "keep"  # 返回

    @make_tool  # 声明为工具
    def skip() -> str:  # 不该收
        """跳过工具。"""
        return "skip"  # 返回

    module = SimpleNamespace(keep=keep, skip=skip, __all__=["keep"])  # 声明 __all__
    assert [tool.name for tool in collect(module)] == ["keep"]  # 只收声明