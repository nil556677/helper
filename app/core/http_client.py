"""共享出站 HTTP 客户端：统一超时、重试、JSON 编解码。

对外发请求的地方（工具/服务等）统一用它，不再各自写 httpx/urllib（天气/三方 API 等）。
复用 `core.retry` 的错误分类与指数退避：**只对瞬时类错误（限流/5xx/超时/断连）自动重试**；
配置/未知错误照常抛出，由调用方按需转成 `ResultEnvelope`。
提供异步（带重试）与同步（带轻量重试）两组入口。
"""

from __future__ import annotations  # 延迟求值注解

import time  # 同步重试等待
from typing import Any  # 任意值

import httpx  # HTTP 客户端

from app.core.retry import (  # 通用重试
    RETRYABLE_KINDS,  # 可重试类别
    RetryPolicy,  # 重试策略
    classify_error,  # 错误分类
    retry_async,  # 异步重试
)  # 导入结束

DEFAULT_TIMEOUT = 10.0  # 默认请求超时（秒）

#: 模块级默认重试策略（瞬时类才重试；最多 3 次，指数退避）。
_POLICY = RetryPolicy(max_retries=3, base_delay_s=0.5, max_delay_s=4.0, jitter_s=0.1)  # 策略


# ---------- 异步（带重试） ----------

async def arequest(  # 异步 GET/POST
    method: str,  # 方法（GET/POST/...）
    url: str,  # 地址
    *,
    params: dict[str, Any] | None = None,  # 查询参数
    json: Any = None,  # JSON 体
    headers: dict[str, str] | None = None,  # 请求头
    timeout: float = DEFAULT_TIMEOUT,  # 超时
    on_retry: Any | None = None,  # 每次重试回调（发事件）
) -> httpx.Response:  # 响应
    """带分类重试的异步请求；非 2xx、非瞬时错误照常抛。"""
    async def once() -> httpx.Response:  # 单次请求
        async with httpx.AsyncClient(timeout=timeout) as client:  # 短生命周期客户端
            resp = await client.request(method, url, params=params, json=json, headers=headers)  # 请求
        resp.raise_for_status()  # 非 2xx 抛
        return resp  # 返回
    return await retry_async(once, _POLICY, on_retry=on_retry, scope="http")  # 分类重试


async def aget_json(url: str, **kwargs: Any) -> Any:  # 异步 GET -> JSON
    """GET 并解析 JSON；配合共享重试。"""
    resp = await arequest("GET", url, **kwargs)  # 请求
    return resp.json()  # JSON


async def apost_json(url: str, payload: Any = None, **kwargs: Any) -> Any:  # 异步 POST -> JSON
    """POST 并解析 JSON；配合共享重试。"""
    resp = await arequest("POST", url, json=payload, **kwargs)  # 请求
    return resp.json()  # JSON


# ---------- 同步（带轻量重试） ----------

def _sync_retry(fn: Any) -> httpx.Response:  # 同步重试
    """同步版分类重试：瞬时类才等，用 time.sleep。"""
    attempt = 0  # 已重试次数
    while True:  # 循环
        try:  # 执行
            return fn()  # 成功返回
        except Exception as exc:  # 失败
            kind = classify_error(exc)  # 分类
            if kind not in RETRYABLE_KINDS or attempt >= _POLICY.max_retries:  # 不值得/用尽
                raise  # 原样抛
            attempt += 1  # 计次
            time.sleep(_POLICY.delay_s(attempt))  # 指数退避


def request(  # 同步 GET/POST
    method: str,  # 方法
    url: str,  # 地址
    *,
    params: dict[str, Any] | None = None,  # 查询参数
    json: Any = None,  # JSON 体
    headers: dict[str, str] | None = None,  # 请求头
    timeout: float = DEFAULT_TIMEOUT,  # 超时
) -> httpx.Response:  # 响应
    """带轻量重试的同步请求；非 2xx、非瞬时错误照常抛。"""
    def once() -> httpx.Response:  # 单次
        resp = httpx.request(method, url, params=params, json=json, headers=headers, timeout=timeout)  # 请求
        resp.raise_for_status()  # 非 2xx 抛
        return resp  # 返回
    return _sync_retry(once)  # 重试


def get_json(url: str, **kwargs: Any) -> Any:  # 同步 GET -> JSON
    """GET 并解析 JSON；配合共享重试。"""
    return request("GET", url, **kwargs).json()  # JSON