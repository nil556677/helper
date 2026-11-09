"""统一工具编写范式：作者只写纯函数返回 data，`@tool_spec` 统一收口错误/缓存/并发/元数据。

作者只需要：
- 一个纯函数：``def/async def fn(**参数) -> data``（成功该返回什么就返回什么）；
- 可预期业务错用 ``raise ToolError(code, message, retryable=...)`` 表达；
- 用 ``@tool_spec(description, args_schema, ttl=..., mutation=..., ...)`` 包成 langchain 工具。

装饰器在一处统一处理（7 项优化点）：
1. 错误分流：`ToolError` 转对应 err；上游瞬时错(限流/5xx/超时/断连)转 execution.failed 可重试；
   **其余异常(疑似 BUG/config)记日志并转 internal.error 不可重试**——不让重试掩盖 Bug。
2. 并发 single-flight：同参在算时，后来的调用等它，不重复跑（尤其 RAG 重）。
3. 元数据随工具走：`mutation/permissions/version` 挂到工具上，供 describe/审计/重试策略读取。
4. 缓存命中可辨识：命中返回 `ok(data, cached=True)`。
5. 缓存键归一：用 `args_schema` 补默认值后再生成键，避免同参因默认值缺失成两个键。
6. 可测性：结构可分离（`_error_envelope`/`_compute_*`），并配套专项单测。
7. 校验错转消息：`handle_validation_error=True`，参数不合法返回 message 而非抛异常。
"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 判断协程 / single-flight Future
import json  # 参数规范化成缓存键
import threading  # 同步 single-flight
from inspect import isawaitable  # 判断可等待
from typing import Any, Callable  # 类型

from langchain_core.tools import StructuredTool  # 框架工具（返回包络）
from pydantic import BaseModel  # 参数契约基类

from app.agent.contracts import Mutation  # 变更画像
from app.core.error import ResolveError, ok, resolve  # 统一错误基类/成功/分类
from app.agent.tools.cache import TtlCache  # TTL 缓存（窗口内幂等）
from app.agent.tools.wiring import render, to_content_and_artifact  # 模型视角/审计视角拆分

#: single-flight 等待上限（秒）：领导者没在时限内完成就自己算，避免永久阻塞。
SINGLEFLIGHT_WAIT_S = 4.0  # 默认等待（工具超时 5s 时留 1s 给收尾）


class ToolError(ResolveError):  # 工具业务错
    """工具作者用它表达可预期失败；继承 `core.error.ResolveError`，分类/求解由 `resolve` 统一。"""


def _args_key(kwargs: dict[str, Any], schema: type[BaseModel] | None = None) -> str:  # 参数 -> 缓存键
    """用 args_schema 补默认值后再 json 化，保证「逻辑同参」稳定命中（优化点 5）。"""
    payload: Any = kwargs  # 默认原参
    if schema is not None:  # 有 schema
        try:  # 校验补默认
            payload = schema(**kwargs).model_dump()  # 规范化（含默认值）
        except Exception:  # 校验不过
            payload = kwargs  # 用原参兜底
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)  # 稳定键


async def _maybe_await(value: Any) -> Any:  # 需要时 await
    """纯函数可能是同步或异步；是协程/可等待就取回结果。"""
    if isawaitable(value):  # 可等待
        return await value  # 取
    return value  # 原样


def _error_envelope(exc: BaseException, *, where: str = "") -> Any:  # 异常 → 统一 Result
    """委托 `core.error.resolve`（唯一分类出处）：ToolError 业务错、上游瞬时可重试、Bug 不可重试。"""
    return resolve(exc, where=where or "tool")  # 核心统一分类


def _validate_result(data: Any, result_schema: type[BaseModel] | None) -> None:  # 运行时校验返回形状
    """声明了 result_schema 就必须通过校验；不通过按编程 Bug 处理（不吞）。"""
    if result_schema is None:  # 未声明
        return  # 跳过
    if isinstance(result_schema, type) and issubclass(result_schema, BaseModel):  # pydantic 模型
        result_schema.model_validate(data)  # 形状不对会抛


async def _compute_async(fn: Callable[..., Any], cache: TtlCache | None, key: str, kwargs: dict[str, Any], result_schema: type[BaseModel] | None = None) -> Any:  # 异步计算
    """执行纯函数 + 缓存写入 + 形状校验 + 错误映射；返回包络。"""
    try:  # 成功 / 业务错 / 内部或上游错
        data = await _maybe_await(fn(**kwargs))  # 调纯函数
        _validate_result(data, result_schema)  # 校验返回形状
    except Exception as exc:  # 统一映射（优化点 1 在 _error_envelope）
        return _error_envelope(exc, where=getattr(fn, "__name__", "tool"))  # 包络
    if cache is not None:  # 只缓存成功
        cache.set(key, data)  # 存
    return ok(data, cached=False)  # 成功包络（非命中）

def _compute_sync(fn: Callable[..., Any], cache: TtlCache | None, key: str, kwargs: dict[str, Any], result_schema: type[BaseModel] | None = None) -> Any:  # 同步计算
    """同步版：返回包络；与异步版共享错误映射与形状校验。"""
    try:  # 成功 / 业务错 / 内部或上游错
        data = fn(**kwargs)  # 调纯函数（同步）
        _validate_result(data, result_schema)  # 校验返回形状
    except Exception as exc:  # 统一映射
        return _error_envelope(exc, where=getattr(fn, "__name__", "tool"))  # 包络
    if cache is not None:  # 只缓存成功
        cache.set(key, data)  # 存
    return ok(data, cached=False)  # 成功包络

#: 异步 single-flight：key -> Future（让并发同参等一个）。
_inflight: dict[tuple, asyncio.Future] = {}
#: 同步 single-flight：key -> Event（线程间去重）。
_inflight_sync: dict[tuple, threading.Event] = {}
_inflight_lock = threading.Lock()  # 同步字典锁
_singleflight_timeout_count = 0  # 等待超时降级自算的次数（可观测）
_singleflight_count_lock = threading.Lock()  # 计数器锁


def singleflight_timeouts() -> int:  # 等待超时次数
    """返回进程内 single-flight 等待超时、降级自算的累计次数（观测用）。"""
    return int(_singleflight_timeout_count)  # 读计数


def _mark_timeout() -> None:  # 记一次超时
    """线程安全自增超时计数。"""
    global _singleflight_timeout_count  # 改模块级
    with _singleflight_count_lock:  # 加锁
        _singleflight_timeout_count += 1  # 自增


def _find_hit(cache: TtlCache | None, key: str) -> Any | None:  # 查缓存命中
    """命中返回包络，否则 None；顺带让命中标记可见（优化点 4）。"""
    if cache is None:  # 没开缓存
        return None  # 无命中
    cached = cache.get(key)  # 查
    return ok(cached, cached=True) if cached is not None else None  # 命中带标记


async def _call_async(fn: Callable[..., Any], cache: TtlCache | None, schema: type[BaseModel] | None, wait_s: float, result_schema: type[BaseModel] | None = None, **kwargs: Any) -> Any:  # 异步主入口
    """缓存 + single-flight + 计算；并发同参合并为一次（优化点 2），等待超时则自算。"""
    key = _args_key(kwargs, schema)  # 规范参数键（缓存用小 key）
    hit = _find_hit(cache, key)  # 命中即回
    if hit is not None:  # 命中
        return hit  # 带 cached 标记
    dkey = (getattr(fn, "__module__", ""), getattr(fn, "__qualname__", "tool"), key)  # 含模块/限定名，避免跨模块同名碰撞
    fut = _inflight.get(dkey)  # 有别的调用在算？
    if fut is not None:  # 有
        try:  # 等它，但不无限等
            await asyncio.wait_for(fut, timeout=wait_s)  # 超时后放弃等待
        except (asyncio.TimeoutError, asyncio.CancelledError):  # 领导者卡住/被取消
            _mark_timeout()  # 记一次超时（观测）
            pass  # 降级为自算
        hit = _find_hit(cache, key)  # 它成功后已落缓存
        if hit is not None:  # 命中了
            return hit  # 直接回
        return await _compute_async(fn, cache, key, kwargs, result_schema)  # 它失败了，保守自算
    loop = asyncio.get_running_loop()  # 当前循环
    fut = loop.create_future()  # 领导者信号
    _inflight[dkey] = fut  # 占位
    try:  # 计算
        return await _compute_async(fn, cache, key, kwargs, result_schema)  # 领导计算
    finally:  # 一定释放
        if not fut.done():  # 没被其它收尾
            fut.set_result(None)  # 唤醒等待者
        _inflight.pop(dkey, None)  # 移除占位


def _call_sync(fn: Callable[..., Any], cache: TtlCache | None, schema: type[BaseModel] | None, wait_s: float, result_schema: type[BaseModel] | None = None, **kwargs: Any) -> Any:  # 同步主入口
    """同步版：缓存 + single-flight + 计算（线程安全，等待有超时）。"""
    key = _args_key(kwargs, schema)  # 规范参数键
    hit = _find_hit(cache, key)  # 命中即回
    if hit is not None:  # 命中
        return hit  # 带 cached 标记
    dkey = (getattr(fn, "__module__", ""), getattr(fn, "__qualname__", "tool"), key)  # 含模块/限定名
    with _inflight_lock:  # 线程安全查占位
        event = _inflight_sync.get(dkey)  # 有别的在算？
        not_leader = event is not None  # 是否要等
        if not not_leader:  # 我是领导者
            event = threading.Event()  # 建信号
            _inflight_sync[dkey] = event  # 占位
    if not_leader:  # 等领导者
        if not event.wait(wait_s):  # 有超时的等，避免永久阻塞
            _mark_timeout()  # 超时了：记一次并降级自算
        hit = _find_hit(cache, key)  # 它成功后已落缓存
        if hit is not None:  # 命中
            return hit  # 回
    try:  # 计算（含领导者）
        return _compute_sync(fn, cache, key, kwargs, result_schema)  # 计算
    finally:  # 一定释放
        with _inflight_lock:  # 加锁
            _inflight_sync.pop(dkey, None)  # 移除占位
        event.set()  # 唤醒等待者


def tool_spec(  # 工具范式装饰器
    *,
    description: str,  # 能力说明（给模型）
    args_schema: type[BaseModel],  # 参数契约（严格 schema）
    ttl: float | None = None,  # 可选缓存窗口（秒）；None = 不缓存
    mutation: Mutation = Mutation.READ_ONLY,  # 变更画像（重试安全决策）
    version: int = 1,  # 契约版本（破坏性变更 +1）
    permissions: tuple[str, ...] = (),  # 权限标签
    result_schema: type[BaseModel] | None = None,  # 结果契约（describe/审计/下游校验用）
    approval: str = "none",  # 审批策略（manual=必经人工审批 / auto / none）
    risk: str = "low",  # 风险等级（low/medium/high）
    timeout_ms: int = 5000,  # 工具级超时（毫秒；框架可读）
    max_entries: int | None = 1000,  # 缓存容量上限（防长期增长；None=不限）
) -> Callable[[Callable[..., Any]], StructuredTool]:  # 返回装饰器
    """包成返回 `(模型视角, 审计视角)` 的 langchain 工具；错误/缓存/并发/元数据在此统一。"""
    def deco(fn: Callable[..., Any]) -> StructuredTool:  # 装饰器本体
        cache = TtlCache(ttl=float(ttl), max_entries=max_entries) if ttl is not None else None  # 有界缓存
        wait_s = max(0.1, float(timeout_ms) / 1000.0 - 0.5)  # 等待上限略小于工具超时，留收尾余量

        async def wrapped_async(**kwargs: Any) -> Any:  # 异步入口（真协程函数）
            env = await _call_async(fn, cache, args_schema, wait_s, result_schema, **kwargs)  # 主路由
            return to_content_and_artifact(env, render(env))  # 模型只拿 data，审计留完整包络

        def wrapped_sync(**kwargs: Any) -> Any:  # 同步入口（真函数）
            env = _call_sync(fn, cache, args_schema, wait_s, result_schema, **kwargs)  # 主路由
            return to_content_and_artifact(env, render(env))  # 同上

        common = {  # 公共参数
            "name": fn.__name__,  # 名 = 函数名
            "description": description,  # 能力说明
            "args_schema": args_schema,  # 参数契约
            "handle_validation_error": True,  # 校验错转 message 而非抛（优化点 7）
            "response_format": "content_and_artifact",  # 模型视角与审计视角分离
        }  # 公共参数结束
        if asyncio.iscoroutinefunction(fn):  # 异步函数 -> 异步工具
            tool = StructuredTool(**common, coroutine=wrapped_async)  # 只异步
        else:  # 同步函数 -> 同步/异步都支持
            tool = StructuredTool(**common, func=wrapped_sync, coroutine=wrapped_async)  # 双入口
        # 元数据随工具走（优化点 3）：describe/审计/重试策略能直接读到（放在合法字段 metadata）
        existing = dict(tool.metadata or {})  # 已有的元数据
        existing["dsh"] = {  # 项目元数据
            "mutation": mutation,  # 变更画像
            "approval": str(approval),  # 审批策略
            "risk": str(risk),  # 风险等级
            "version": int(version),  # 版本
            "permissions": tuple(permissions),  # 权限
            "result_schema": result_schema,  # 结果契约
            "timeout_ms": int(timeout_ms),  # 工具超时
        }  # 项目元数据结束
        tool.metadata = existing  # 写回（BaseTool.metadata 是合法 pydantic 字段）
        return tool  # 返回工具

    return deco  # 返回装饰器


def unwrap_tool_output(value: Any) -> Any:  # (content, artifact) → 完整 Result
    """程序化调用（tool.invoke）拿到的是 (content, artifact)，这里还原成 Result 供代码读 data。"""
    from app.core.result import Result  # 唯一结果契约

    if isinstance(value, Result):  # 已是包络
        return value  # 原样
    if isinstance(value, tuple) and len(value) == 2:  # content_and_artifact
        artifact = value[1]  # 审计视角
        if isinstance(artifact, Result):  # 已是对象
            return artifact  # 原样
        if isinstance(artifact, str):  # 序列化文本
            try:  # 解析
                return Result.model_validate_json(artifact)  # 还原
            except Exception:  # 解析失败
                return value  # 退回原值
    return value  # 其它原样