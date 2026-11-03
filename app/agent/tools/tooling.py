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
from loguru import logger  # 内部 Bug 记日志
from pydantic import BaseModel  # 参数契约基类

from app.agent.contracts import ErrorCode, Mutation  # 错误码 / 变更画像
from app.core.retry import RETRYABLE_KINDS, classify_error  # 上游错误分类
from app.agent.tools.cache import TtlCache  # TTL 缓存（窗口内幂等）
from app.agent.tools.wiring import err, ok  # 统一包络


class ToolError(Exception):  # 业务错
    """携带错误码与是否可重试；由 `tool_spec` 转成 err 包络。

    工具作者用它表达「可预期的失败」（城市没查到、格式不对等）；其余异常走内部/执行层分流。
    """

    def __init__(  # 构造
        self,  # 异常
        code: str,  # 错误码（用 ErrorCode 常量）
        message: str = "",  # 人话说明（给模型/调用方）
        *,
        retryable: bool = False,  # 是否可重试
        retry_after_s: float | None = None,  # 建议等待秒数
    ) -> None:  # 无返回
        self.code = code or str(ErrorCode.EXECUTION_FAILED)  # 错误码（防空）
        self.retryable = retryable  # 可重试标记
        self.retry_after_s = retry_after_s  # 建议等待
        super().__init__(message)  # 交给 Exception

    @property  # 只读属性
    def message(self) -> str:  # 取说明
        """把 Exception 的 args[0] 当 message 用。"""
        return str(self.args[0])  # 返回


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


def _error_envelope(exc: BaseException, *, where: str = "") -> Any:  # 异常 -> 包络（优化点 1）
    """错误统一映射（唯一一处）：
    - `ToolError` -> 业务 err；
    - 上游瞬时错（分类命中）-> execution.failed 可重试；
    - 其余（疑似 BUG/config）-> 记日志 + internal.error 不可重试。
    """
    if isinstance(exc, ToolError):  # 业务错
        return err(  # 带内
            str(exc.code),  # 错误码
            exc.message,  # 说明
            retryable=exc.retryable,  # 可重试
            retry_after_s=exc.retry_after_s,  # 建议等待
        )  # 包络结束
    kind = classify_error(exc)  # 分类（复用 llm.retry）
    if kind in RETRYABLE_KINDS:  # 上游瞬时类
        return err(str(ErrorCode.EXECUTION_FAILED), str(exc)[:300], retryable=True)  # 可重试
    logger.opt(exception=exc).error("工具内部错误（{}）{}", where or "tool", str(exc)[:200])  # 记真 Bug
    return err(str(ErrorCode.INTERNAL_ERROR), str(exc)[:300], retryable=False)  # 不可重试，暴露问题


async def _compute_async(fn: Callable[..., Any], cache: TtlCache | None, key: str, kwargs: dict[str, Any]) -> Any:  # 异步计算
    """执行纯函数 + 缓存写入 + 错误映射；返回包络。"""
    try:  # 成功 / 业务错 / 内部或上游错
        data = await _maybe_await(fn(**kwargs))  # 调纯函数
    except Exception as exc:  # 统一映射（优化点 1 在 _error_envelope）
        return _error_envelope(exc, where=getattr(fn, "__name__", "tool"))  # 包络
    if cache is not None:  # 只缓存成功
        cache.set(key, data)  # 存
    return ok(data, cached=False)  # 成功包络（非命中）

def _compute_sync(fn: Callable[..., Any], cache: TtlCache | None, key: str, kwargs: dict[str, Any]) -> Any:  # 同步计算
    """同步版：返回包络；与异步版共享错误映射。"""
    try:  # 成功 / 业务错 / 内部或上游错
        data = fn(**kwargs)  # 调纯函数（同步）
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


def _find_hit(cache: TtlCache | None, key: str) -> Any | None:  # 查缓存命中
    """命中返回包络，否则 None；顺带让命中标记可见（优化点 4）。"""
    if cache is None:  # 没开缓存
        return None  # 无命中
    cached = cache.get(key)  # 查
    return ok(cached, cached=True) if cached is not None else None  # 命中带标记


async def _call_async(fn: Callable[..., Any], cache: TtlCache | None, schema: type[BaseModel] | None, **kwargs: Any) -> Any:  # 异步主入口
    """缓存 + single-flight + 计算；并发同参合并为一次（优化点 2）。"""
    key = _args_key(kwargs, schema)  # 规范参数键（缓存用小 key）
    hit = _find_hit(cache, key)  # 命中即回
    if hit is not None:  # 命中
        return hit  # 带 cached 标记
    dkey = (getattr(fn, "__name__", "tool"), key)  # single-flight 唯一键（含工具名）
    fut = _inflight.get(dkey)  # 有别的调用在算？
    if fut is not None:  # 有
        await fut  # 等它
        hit = _find_hit(cache, key)  # 它成功后已落缓存
        if hit is not None:  # 命中了
            return hit  # 直接回
        return await _compute_async(fn, cache, key, kwargs)  # 它失败了，保守自算
    loop = asyncio.get_running_loop()  # 当前循环
    fut = loop.create_future()  # 领导者信号
    _inflight[dkey] = fut  # 占位
    try:  # 计算
        return await _compute_async(fn, cache, key, kwargs)  # 领导计算
    finally:  # 一定释放
        if not fut.done():  # 没被其它收尾
            fut.set_result(None)  # 唤醒等待者
        _inflight.pop(dkey, None)  # 移除占位


def _call_sync(fn: Callable[..., Any], cache: TtlCache | None, schema: type[BaseModel] | None, **kwargs: Any) -> Any:  # 同步主入口
    """同步版：缓存 + single-flight + 计算（线程安全）。"""
    key = _args_key(kwargs, schema)  # 规范参数键
    hit = _find_hit(cache, key)  # 命中即回
    if hit is not None:  # 命中
        return hit  # 带 cached 标记
    dkey = (getattr(fn, "__name__", "tool"), key)  # 唯一键
    with _inflight_lock:  # 线程安全查占位
        event = _inflight_sync.get(dkey)  # 有别的在算？
        not_leader = event is not None  # 是否要等
        if not not_leader:  # 我是领导者
            event = threading.Event()  # 建信号
            _inflight_sync[dkey] = event  # 占位
    if not_leader:  # 等领导者
        event.wait()  # 阻塞等
        hit = _find_hit(cache, key)  # 它成功后已落缓存
        if hit is not None:  # 命中
            return hit  # 回
    try:  # 计算（含领导者）
        return _compute_sync(fn, cache, key, kwargs)  # 计算
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
) -> Callable[[Callable[..., Any]], StructuredTool]:  # 返回装饰器
    """包成返回 `ResultEnvelope` 的 langchain 工具；错误/缓存/并发/元数据在此统一。"""
    def deco(fn: Callable[..., Any]) -> StructuredTool:  # 装饰器本体
        cache = TtlCache(ttl=float(ttl)) if ttl is not None else None  # 可选缓存

        async def wrapped_async(**kwargs: Any) -> Any:  # 异步入口（真协程函数）
            return await _call_async(fn, cache, args_schema, **kwargs)  # 主路由

        def wrapped_sync(**kwargs: Any) -> Any:  # 同步入口（真函数）
            return _call_sync(fn, cache, args_schema, **kwargs)  # 主路由

        common = {  # 公共参数
            "name": fn.__name__,  # 名 = 函数名
            "description": description,  # 能力说明
            "args_schema": args_schema,  # 参数契约
            "handle_validation_error": True,  # 校验错转 message 而非抛（优化点 7）
        }  # 公共参数结束
        if asyncio.iscoroutinefunction(fn):  # 异步函数 -> 异步工具
            tool = StructuredTool(**common, coroutine=wrapped_async)  # 只异步
        else:  # 同步函数 -> 同步/异步都支持
            tool = StructuredTool(**common, func=wrapped_sync, coroutine=wrapped_async)  # 双入口
        # 元数据随工具走（优化点 3）：describe/审计/重试策略能直接读到（放在合法字段 metadata）
        existing = dict(tool.metadata or {})  # 已有的元数据
        existing["dsh"] = {  # 项目元数据
            "mutation": mutation,  # 变更画像
            "version": int(version),  # 版本
            "permissions": tuple(permissions),  # 权限
        }  # 项目元数据结束
        tool.metadata = existing  # 写回（BaseTool.metadata 是合法 pydantic 字段）
        return tool  # 返回工具

    return deco  # 返回装饰器