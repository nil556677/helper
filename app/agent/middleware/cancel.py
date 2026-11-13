"""取消中间件：在 run 生命周期开始时检查取消信号。

逐调用的取消（流式 token 检查）由框架 AgentMiddleware `CancelAgentMiddleware`
（`app/agent/middleware/agent_mw.py`）承接——那套自研的 `wrap_model_call` 包裹未接线，
已随 `MiddlewareChain.model_call` 一并移除。
"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.context import RunContext  # 运行上下文
from app.agent.middleware.base import Middleware  # 中间件基类


class CancelMiddleware(Middleware):  # 取消检查
    """run 开始前就把已请求的取消尽早抛出。"""

    name = "cancel"  # 名称

    async def before_run(self, ctx: RunContext) -> None:  # run 前
        """开始前如果已经请求取消，直接退出。"""
        ctx.check_cancelled()  # 可能抛 RunCancelled
