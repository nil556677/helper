"""统一请求/响应 DTO。

- 请求体用 pydantic ``BaseModel``（FastAPI 自动校验 422）；
- 成功响应统一为 envelope（code/message/request_id/data），见 :func:`ok`；
- 业务码见 ``app.http.errors``。
"""
from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from pydantic import BaseModel, Field  # 校验模型


class RunCreateRequest(BaseModel):  # 创建一轮（发送消息）
    """发送一条消息的请求。"""

    question: str = Field(min_length=1)  # 问题文本
    conversation_id: str = "default"  # 会话 id
    attachments: list[dict[str, Any]] = Field(default_factory=list)  # 附件元数据


class ApproveRequest(BaseModel):  # 审批请求
    """对一次审批请求给结论。"""

    request_id: str  # 请求 id
    allow: bool  # 是否允许


def ok(data: Any = None, *, code: int = 0, message: str = "ok", request_id: str = "") -> dict:  # 成功体
    """构造统一成功响应。"""
    return {  # 返回
        "code": code,  # 业务码
        "message": message,  # 消息
        "request_id": request_id,  # 追踪 id
        "data": data,  # 载荷
    }  # 结束
