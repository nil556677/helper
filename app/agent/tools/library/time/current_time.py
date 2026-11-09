"""时间工具：按统一范式编写 —— 纯函数返回 data，错误用 `ToolError`；由 @tool_spec 收口。

作者只写「成功该返回什么」与「何时抛业务错」，不做异常捕获/缓存/包络（都由 tool_spec 统一处理）。
"""

from __future__ import annotations  # 延迟求值注解

import time  # 秒级时间戳
from datetime import datetime  # 本地时间

from pydantic import BaseModel, Field  # 参数契约（会随 args_schema 发给模型）

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.tools.tooling import ToolError, tool_spec  # 统一范式

DEFAULT_FORMAT = "%Y-%m-%d %H:%M:%S"  # 默认格式（如 2026-11-03 17:38:30）

#: 参数契约：字段说明只写 Field(description=...)，不写行尾注释（模型只能读到 description）。
class CurrentTimeParams(BaseModel):  # 参数契约
    format: str = Field(  # 格式参数
        default=DEFAULT_FORMAT,  # 默认格式
        description="时间输出格式，Python strftime 语法；默认 %Y-%m-%d %H:%M:%S",  # 给模型的说明
    )  # 字段结束
    as_timestamp: bool = Field(  # 是否要时间戳
        default=False,  # 默认不要
        description="是否改为返回秒级 Unix 时间戳；为真时忽略 format",  # 给模型的说明
    )  # 字段结束


@tool_spec(  # 统一范式：成功返回 ok 包络、业务错转带内 err
    description="查询当前本地时间，按指定 strftime 格式，或返回秒级 Unix 时间戳。",  # 能力说明
    args_schema=CurrentTimeParams,  # 参数契约
)  # 装饰结束
def current_time(format: str | None = None, as_timestamp: bool = False):  # 纯函数（返回 data）
    """成功返回 data 字典；格式不合法抛 ToolError（由 tool_spec 转带内 err）。

    `format=None` 时用模块常量 DEFAULT_FORMAT（默认值单一来源）。
    """
    fmt = format or DEFAULT_FORMAT  # 单一来源：签名不再重复默认值
    ts = int(time.time())  # 秒级时间戳（内部统一给，方便调用方直接用）
    if as_timestamp:  # 只要时间戳
        return {"value": str(ts), "kind": "timestamp", "ts": ts, "format": ""}  # data
    try:  # 格式不合法会抛
        text = datetime.now().strftime(fmt)  # 文本
    except (ValueError, TypeError) as exc:  # 格式错
        raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), f"时间格式不合法：{exc}")  # 业务错
    return {"value": text, "kind": "datetime", "ts": ts, "format": fmt}  # data