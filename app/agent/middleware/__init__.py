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
from app.agent.middleware.trace import TraceMiddleware  # 追踪


def default_chain() -> MiddlewareChain:  # 默认链
    """顺序：Trace → Cancel → PII。

    历史裁剪由上下文层（`contexts.history.trim`）负责，已在 load_context 完成；
    工具执行改由 langgraph 的 ``ToolNode`` 承担（见 `agent.tools.wiring`），
    这里不再有工具中间件，工具计数/限额在接入真实工具后由 wiring 统一处理。
    """
    return MiddlewareChain(  # 构造链
        [  # 中间件列表
            TraceMiddleware(),  # 最外层：计时
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
    "TraceMiddleware",  # 追踪
    "default_chain",  # 默认链
]  # 导出结束