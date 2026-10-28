"""工具接口契约：能力描述、结果包络、错误码、变更画像。

只放「描述符」（name/description/schema/mutation/permissions）；handler 属实现，不进这里。
"""

from __future__ import annotations  # 类型注解延迟求值

from enum import StrEnum  # 字符串枚举
from typing import Any  # 任意值

from pydantic import BaseModel, Field  # 校验模型


class Mutation(StrEnum):  # 变更画像
    """指导框架在重试时的安全决策。"""

    READ_ONLY = "read_only"  # 只读，可安全重试
    IDEMPOTENT_WRITE = "idempotent_write"  # 幂等写，可安全重试
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"  # 非幂等写，重试需确认


class ToolDescriptor(BaseModel):  # 工具对外契约（描述符）
    """单文件单工具（`@tool`），name = id = 动词_对象。"""

    name: str  # 工具名 = id（[a-z0-9_]，无点号）
    version: int = 1  # 递增；破坏性变更 +1
    description: str = ""  # 能力 / 场景 / 边界（四要素）
    args_schema: Any = None  # 参数 Pydantic schema
    result_schema: Any = None  # 结果 schema
    mutation: Mutation = Mutation.READ_ONLY  # 变更画像
    permissions: tuple[str, ...] = Field(default_factory=tuple)  # 权限标签


class ResultEnvelope(BaseModel):  # 工具 / 任务统一返回包络
    """带内错误（不抛业务异常），让模型能据返回推理下一步。"""

    ok: bool  # 是否成功
    code: str  # 错误码（protocol.* / execution.*）
    message: str = ""  # 人话信息
    retryable: bool = False  # 是否可重试
    retry_after_s: float | None = None  # 建议等待秒数
    data: Any = None  # 结果数据
    meta: dict[str, Any] = Field(default_factory=dict)  # 元数据


class ErrorCode:  # 错误码常量（唯一分类来源）
    """输出层、事件层、retry 分类都引用这些 code 字符串（跨域只走 ID，不 import）。"""

    # 协议层：工具不可用 / 参数错 / 无权限（不重试）
    PROTOCOL_TOOL_NOT_FOUND = "protocol.tool_not_found"  # 工具不存在
    PROTOCOL_ARG_INVALID = "protocol.arg_invalid"  # 参数非法
    PROTOCOL_DISABLED = "protocol.disabled"  # 已被禁用
    PROTOCOL_PERMISSION = "protocol.permission"  # 无权限
    # 执行层：运行失败 / 限流 / 超时（限流超时可重试）
    EXECUTION_FAILED = "execution.failed"  # 执行失败
    EXECUTION_RATE_LIMIT = "execution.rate_limit"  # 被限流（可等后重试）
    EXECUTION_TIMEOUT = "execution.timeout"  # 超时

    @staticmethod  # 静态方法
    def is_protocol(code: str) -> bool:  # 是否协议层错误
        return code.startswith("protocol.")  # 前缀判断
