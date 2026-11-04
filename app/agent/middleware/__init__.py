"""中间件包：默认链顺序固定，业务只加不改顺序。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.middleware.base import (  # 契约
    Middleware,
    MiddlewareChain,
    ModelRequest,
    ModelResponse,
)
from app.agent.middleware.cancel import CancelMiddleware  # 取消
from app.agent.middleware.pii import PIIMiddleware  # 脱敏


def default_chain() -> MiddlewareChain:  # 默认链
    """顺序：Cancel → PII（仅 before/after 钩子；模型/工具调用已交 create_agent 官方中间件）。"""
    return MiddlewareChain(  # 构造链
        [  # 中间件列表
            CancelMiddleware(),  # 尽早短路
            PIIMiddleware(),  # 出站脱敏
        ]  # 列表结束
    )  # 返回链


__all__ = [  # 导出
    "CancelMiddleware",  # 取消
    "Middleware",  # 基类
    "MiddlewareChain",  # 链
    "ModelRequest",  # 模型请求
    "ModelResponse",  # 模型响应
    "PIIMiddleware",  # 脱敏
    "default_chain",  # 默认链
]  # 导出结束