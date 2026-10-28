"""E4 工具契约核心：ToolSpec / Registry / 包络 / 序列化。"""

from __future__ import annotations  # 类型注解延迟求值

import datetime  # 时间
import decimal  # 十进制

from pydantic import BaseModel  # 校验

from app.agent.tool_contract import (  # 被测
    EXECUTION,
    PROTOCOL,
    ToolRegistry,
    ToolSpec,
    error_envelope,
    safe_serialize,
    success_envelope,
)


class _Args(BaseModel):  # 参数契约
    query: str  # 检索词


def _handler(query: str) -> dict:  # 假实现
    return {"n": len(query)}  # 返回


def _spec(name: str, version: int, **kw) -> ToolSpec:  # 造 spec
    defaults: dict = {"args_schema": _Args}  # 默认
    defaults.update(kw)  # 覆盖
    return ToolSpec(name=name, version=version, handler=_handler, **defaults)  # 返回


def test_register_resolve_latest() -> None:  # 注册/解析最新
    reg = ToolRegistry()  # 注册表
    reg.register(_spec("search_kb", 1))  # v1
    reg.register(_spec("search_kb", 2))  # v2
    assert reg.resolve("search_kb").version == 2  # 最新
    assert reg.resolve("search_kb", 1).version == 1  # 指定
    assert reg.resolve("missing") is None  # 不存在
    assert reg.versions("search_kb") == [1, 2]  # 版本列表


def test_register_duplicate_raises() -> None:  # 同版本防撞
    reg = ToolRegistry()  # 注册表
    reg.register(_spec("t", 1))  # v1
    try:  # 期待抛
        reg.register(_spec("t", 1))  # 再注册
    except ValueError:  # 捕获
        pass  # 通过
    else:  # 没抛
        raise AssertionError("duplicate register should raise")  # 失败


def test_allowlist_filters_permissions_and_disabled() -> None:  # 白名单
    reg = ToolRegistry()  # 注册
    reg.register(_spec("kb", 1, permissions=("reader",)))  # 读者工具
    reg.register(_spec("fs", 1, permissions=("admin",)))  # 文件工具
    reg.register(_spec("off", 1, enabled=False))  # 禁用
    got = reg.allowlist(("reader",))  # 读者 agent
    names = {s.name for s in got}  # 名字
    assert names == {"kb"}  # 只见 kb


def test_envelope_success_and_protocol() -> None:  # 包络
    ok = success_envelope({"a": 1})  # 成功
    assert ok["ok"] is True and ok["code"] == "ok"  # 标志
    err = error_envelope(  # 协议错误
        f"{PROTOCOL}.unknown_tool",  # code
        "工具不存在，请改调用。",  # message
        retryable=False,  # 不重试
    )  # 错误
    assert err["ok"] is False  # 失败
    assert err["code"] == f"{PROTOCOL}.unknown_tool"  # 前缀
    assert err["retryable"] is False  # 不重试


def test_exception_envelope_retryable() -> None:  # 执行错误可重试
    err = error_envelope(  # 构造
        f"{EXECUTION}.rate_limited",  # code
        "Rate limit exceeded. Retry after 30 seconds",  # 可恢复文案
        retryable=True,  # 可重试
        retry_after_s=30.0,  # 等待
    )  # 构造
    assert err["retryable"] is True  # 可重试
    assert err["retry_after_s"] == 30.0  # 建议等待


def test_safe_serialize_converts() -> None:  # 序列化
    raw = {  # 混合
        "d": datetime.date(2026, 10, 27),  # 日期
        "dec": decimal.Decimal("3.5"),  # 十进制
        "b": b"hi",  # 字节
        "s": {"b", "a"},  # 集合
    }  # 原始
    out = safe_serialize(raw)  # 转换
    assert out["d"] == "2026-10-27"  # ISO
    assert out["dec"] == "3.5"  # 字符串
    assert out["b"] == "hi"  # 解码
    assert out["s"] == ["a", "b"]  # 排序列表
