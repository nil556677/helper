"""异常 → 统一 `Result` 的分类与求解（全项目唯一出处）。

- `ResolveError`：**业务错的基类**(各模块可继承出自己的业务错，如工具 `ToolError`)，携带
  code/message/retryable/retry_after_s。
- `resolve(exc)`：异常 → `Result`：业务错按自身信息；系统错用 `classify_error`：
  瞬时（限流/5xx/超时/断连）→ `execution.failed` 可重试；其余（配置/未知/Bug）→
  `internal.error` 不可重试 + 记日志（不让重试掩盖 Bug）。
- `ok / err`：构造成功 / 失败 `Result`。
所有层都从这里拿"正确/错误 + 是否重试"，不再各写各的映射。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from loguru import logger  # 内部 Bug 记日志

from app.agent.contracts import ErrorCode  # 错误码单源
from app.core.result import Result  # 唯一结果契约
from app.core.retry import RETRYABLE_KINDS, classify_error  # 上游错误分类


class ResolveError(Exception):  # 业务错基类
    """可预期失败的基类，模块可继承出自己的业务错。"""

    def __init__(  # 构造
        self,  # 异常
        code: str,  # 错误码（ErrorCode 常量）
        message: str = "",  # 人话说明
        *,
        retryable: bool = False,  # 是否可重试
        retry_after_s: float | None = None,  # 建议等待
    ) -> None:  # 无返回
        self.code = code or str(ErrorCode.EXECUTION_FAILED)  # 错误码（防空）
        self.retryable = retryable  # 可重试
        self.retry_after_s = retry_after_s  # 建议等待
        super().__init__(message)  # 交给 Exception

    @property  # 只读
    def message(self) -> str:  # 取说明
        return str(self.args[0])  # Exception 第一参


def ok(data: Any = None, **meta: Any) -> Result:  # 成功 Result
    """成功统一入口；`code="ok"`，`meta` 走通用扩展位。"""
    return Result(ok=True, code="ok", data=data, meta=dict(meta))  # 返回


def err(  # 失败 Result
    code: str,  # 错误码
    message: str = "",  # 人话说明
    *,
    retryable: bool = False,  # 是否可重试
    retry_after_s: float | None = None,  # 建议等待
) -> Result:  # 返回
    """带内错误：不抛，让调用方（含模型）据返回推理下一步。"""
    return Result(  # 构造
        ok=False,  # 失败
        code=code,  # 错误码
        message=message,  # 说明
        retryable=retryable,  # 可重试
        retry_after_s=retry_after_s,  # 建议等待
    )  # 返回


def resolve(exc: BaseException, *, where: str = "") -> Result:  # 异常 → Result
    """统一分类：业务 `ResolveError` 按自身；系统错按 `classify_error` 分档。"""
    if isinstance(exc, ResolveError):  # 业务错
        return err(  # 按自身
            str(exc.code),  # 错误码
            exc.message,  # 说明
            retryable=exc.retryable,  # 可重试
            retry_after_s=exc.retry_after_s,  # 建议等待
        )  # 返回
    kind = classify_error(exc)  # 分类（复用 core.retry）
    if kind in RETRYABLE_KINDS:  # 上游瞬时类
        return err(str(ErrorCode.EXECUTION_FAILED), str(exc)[:300], retryable=True)  # 可重试
    logger.opt(exception=exc).error("内部错误（{}）{}", where or "core", str(exc)[:200])  # 记真 Bug
    return err(str(ErrorCode.INTERNAL_ERROR), str(exc)[:300], retryable=False)  # 不可重试，暴露