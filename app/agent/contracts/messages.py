"""内部消息契约：Agent 循环内的消息结构与 tool_call 关联（全 ID）。"""

from __future__ import annotations  # 类型注解延迟求值

from enum import StrEnum  # 字符串枚举
from typing import Any  # 任意值

from pydantic import BaseModel, Field  # 校验模型


class MessageRole(StrEnum):  # 消息角色
    USER = "user"  # 用户
    ASSISTANT = "assistant"  # 助手
    TOOL = "tool"  # 工具结果


class ToolCall(BaseModel):  # 一次工具调用请求
    tool_call_id: str  # 本次调用 id（唯一）
    name: str  # 工具名（动词_对象）
    args: dict[str, Any] = Field(default_factory=dict)  # 入参


class ToolResult(BaseModel):  # 一次工具结果
    """tool_call_id 必须与 ToolCall 匹配，否则历史结构畸形。"""

    tool_call_id: str  # 关联的调用 id
    status: str = "ok"  # ok / error
    content: str = ""  # 喂模型的子集（机器通道）
    artifact: Any = None  # 完整输出（审计通道）
    error_code: str = ""  # 错误码
