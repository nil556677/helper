"""工具接口契约：能力描述、结果包络、错误码、变更画像。

只放「描述符」（name/description/schema/mutation/permissions）；handler 属实现，不进这里。
"""

from __future__ import annotations  # 类型注解延迟求值

from enum import StrEnum  # 字符串枚举
from typing import Any, Literal  # 任意值 / 字面量

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
    args_schema: type[BaseModel] | dict[str, Any] | None = None  # 参数 Pydantic 模型或 JSON Schema
    result_schema: type[BaseModel] | dict[str, Any] | None = None  # 结果模型或 JSON Schema
    mutation: Mutation = Mutation.READ_ONLY  # 变更画像
    approval: Literal["none", "auto", "manual"] = "none"  # 审批策略（manual=必经人工审批 / auto=自动化决策 / none=不审）
    risk: Literal["low", "medium", "high"] = "low"  # 风险等级（影响审批超时/可见性）
    permissions: tuple[str, ...] = Field(default_factory=tuple)  # 权限标签
    timeout_ms: int = 5000  # 工具级超时（毫秒；框架执行时可读它做超时）
    error_schema: type[BaseModel] | dict[str, Any] | None = None  # 错误契约（工具会返回的 code/可重试性）——供模型预判失败形态
    examples: tuple[str, ...] = Field(default_factory=tuple)  # 正反示例（何时用/何时不用），供工具描述补充


class ErrorCode:  # 错误码常量（唯一分类来源）
    """输出层、事件层、retry 分类都引用这些 code 字符串（跨域只走 ID，不 import）。

    命名规则只有两条，新增错误码必须落在其中一条：
    - **带域前缀**：``protocol.*`` / ``execution.*`` / ``internal.*``（工具/业务错）；
    - **扁平码**：``schema_invalid`` 与上游/瞬时分类 ``rate_limit / server / timeout /
      connection / config / unknown``（与 ``core.retry.classify_error`` 输出对齐，
      retry 事件、结构化信封共用）。

    :meth:`is_protocol` 只判断 ``protocol.*``；判断 execution/internal 域请直接
    ``code.startswith("execution.")`` / ``startswith("internal.")``。
    """

    # 协议层：工具不可用 / 参数错 / 无权限（不重试）
    PROTOCOL_TOOL_NOT_FOUND = "protocol.tool_not_found"  # 工具不存在
    PROTOCOL_ARG_INVALID = "protocol.arg_invalid"  # 参数非法
    PROTOCOL_DISABLED = "protocol.disabled"  # 已被禁用
    PROTOCOL_PERMISSION = "protocol.permission"  # 无权限
    PROTOCOL_CAPABILITY_UNSUPPORTED = "protocol.capability_unsupported"  # 无对应专家/能力，显式不兜底
    # 执行层：运行失败 / 限流 / 超时（限流超时可重试）
    EXECUTION_FAILED = "execution.failed"  # 执行失败
    EXECUTION_RATE_LIMIT = "execution.rate_limit"  # 被限流（可等后重试）
    EXECUTION_TIMEOUT = "execution.timeout"  # 超时
    # 结构化：模型输出校验不过（重生成可救，同参重试无益）
    SCHEMA_INVALID = "schema_invalid"  # 结构化输出校验失败
    # 内部层：工具实现 BUG / 未知异常（重试无用，标记不可重试，便于暴露问题）
    INTERNAL_ERROR = "internal.error"  # 内部错误（疑似代码 Bug）
    # 上游/瞬时分类：与 core.retry.classify_error 的输出一一对应（retry 事件、结构化信封共用）
    RATE_LIMIT = "rate_limit"  # 限流 429
    SERVER = "server"  # 服务端 5xx
    TIMEOUT = "timeout"  # 请求超时
    CONNECTION = "connection"  # 连接断开/重置
    CONFIG = "config"  # 配置/鉴权错（400/401/403/404，不重试）
    UNKNOWN = "unknown"  # 认不出的兜底

    @staticmethod  # 静态方法
    def is_protocol(code: str) -> bool:  # 是否协议层错误
        return code.startswith("protocol.")  # 前缀判断
