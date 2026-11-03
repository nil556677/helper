"""通用重试：错误分类 + 指数退避 + 重试事件（通用基础设施，供模型/HTTP/工具等共用）。

只重试「对外请求」的瞬时失败（限流/5xx/超时/连接）；**配置错不重试**——重试三次只会
让用户多等几秒。:func:`retry_async` 与模型无关，embedding / rerank / HTTP / 工具都能直接用。
"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 默认 sleep
import random  # 抖动
from collections.abc import Awaitable, Callable  # 函数签名
from dataclasses import dataclass  # 策略与信息
from typing import Any  # 任意值

#: 值得再试的错误类别（``config`` 故意不在内）。
RETRYABLE_KINDS = frozenset({"rate_limit", "server", "timeout", "connection"})  # 可重试


@dataclass(frozen=True)
class RetryPolicy:  # 重试策略
    """**最多重试 3 次**（不含首次），退避一律指数增长；0.5s 起、上限、带抖动。"""

    max_retries: int = 3  # 最多重试几次（不含第一次请求）
    base_delay_s: float = 0.5  # 第 1 次重试前等多久
    max_delay_s: float = 8.0  # 单次退避上限
    jitter_s: float = 0.2  # 抖动上限（防雪崩）

    def delay_s(self, retry_index: int, *, rng: random.Random | None = None) -> float:  # 本次退避
        """第 ``retry_index`` 次**重试**前等多久：base × 2^(n-1)，封顶后加抖动。"""
        exponent = max(0, retry_index - 1)  # 0,1,2… → base / 2base / 4base
        raw = self.base_delay_s * (2 ** exponent)  # 指数增长
        base = min(raw, self.max_delay_s)  # 封顶
        if self.jitter_s <= 0:  # 不要抖动
            return round(base, 3)  # 原值
        source = rng or random  # 随机源
        return round(base + source.uniform(0.0, self.jitter_s), 3)  # 加抖动


@dataclass(frozen=True)
class RetryInfo:  # 一次重试的事实
    """转成 ``retry`` 事件发给前端（attempt/max_attempts 都按「重试次数」口径）。"""

    attempt: int  # 第几次重试（1..max_attempts）
    max_attempts: int  # 最多重试几次（= policy.max_retries）
    error_kind: str  # 错误类别
    error_message: str  # 原始信息（截断）
    delay_ms: int  # 等多久再试
    scope: str = "model"  # 谁的请求（model / http / structured / embedding…）


def classify_error(exc: BaseException) -> str:  # 错误分类
    """按 HTTP 状态码/异常类型分类；认不出的当 ``unknown``（不重试）。"""
    status = _status_code(exc)  # 状态码
    if status == 429:  # 限流
        return "rate_limit"  # 可重试
    if status is not None and 500 <= status < 600:  # 服务端
        return "server"  # 可重试
    if status in (400, 401, 403, 404):  # 配置/请求错
        return "config"  # 不重试
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):  # 超时
        return "timeout"  # 可重试
    name = type(exc).__name__.lower()  # 异常类名
    if "timeout" in name:  # 各家 SDK 的超时类
        return "timeout"  # 可重试
    if "connect" in name or "connection" in name:  # 连接类
        return "connection"  # 可重试
    return "unknown"  # 兜底


def _status_code(exc: BaseException) -> int | None:  # 取 HTTP 状态码
    """SDK 常把码放 ``status_code``；也兼容 ``response.status_code``。"""
    value = getattr(exc, "status_code", None)  # 直接属性
    if isinstance(value, int):  # 有效
        return value  # 返回
    response = getattr(exc, "response", None)  # 响应对象
    code = getattr(response, "status_code", None)  # 响应上的码
    return code if isinstance(code, int) else None  # 取或空


async def retry_async[T](  # 通用重试
    fn: Callable[[], Awaitable[T]],  # 要执行的动作
    policy: RetryPolicy,  # 策略
    *,
    on_retry: Callable[[RetryInfo], None] | None = None,  # 每次重试的回调（发事件）
    scope: str = "model",  # 作用域（进事件）
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,  # 可注入（测试不真等）
) -> T:  # 结果
    """按策略重试；不可重试/重试用尽时**原样抛出**最后一次异常。"""
    attempt = 0  # 已重试次数（0 = 还没重试过）
    while True:  # 循环直到成功或用尽
        try:  # 执行
            return await fn()  # 成功即返回
        except asyncio.CancelledError:  # 取消必须穿透
            raise  # 不重试
        except Exception as exc:  # 真实失败
            kind = classify_error(exc)  # 分类
            if kind not in RETRYABLE_KINDS or attempt >= policy.max_retries:  # 不值得再试/次数用尽
                raise  # 原样抛
            attempt += 1  # 进入下一次重试
            delay = policy.delay_s(attempt)  # 指数退避
            if on_retry is not None:  # 有回调
                on_retry(  # 报告一次重试
                    RetryInfo(  # 信息
                        attempt=attempt,  # 第几次重试
                        max_attempts=policy.max_retries,  # 最多重试几次
                        error_kind=kind,  # 类别
                        error_message=str(exc)[:300],  # 截断原文
                        delay_ms=int(delay * 1000),  # 毫秒
                        scope=scope,  # 作用域
                    )  # 信息结束
                )  # 回调结束
            await sleep(delay)  # 等一会儿