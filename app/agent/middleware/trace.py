"""追踪中间件：给模型/工具调用计时，真实 trace 写库留给 core/trace 适配器。"""

from __future__ import annotations  # 类型注解延迟求值

import time  # 计时

from loguru import logger  # 调试日志

from app.agent.context import RunContext  # 运行上下文
from app.agent.middleware.base import (  # 中间件契约
    Middleware,
    ModelCall,
    ModelRequest,
    ModelResponse,
)


class TraceMiddleware(Middleware):  # 追踪
    """只观测，不改请求/响应。"""

    name = "trace"  # 名称

    async def before_run(self, ctx: RunContext) -> None:  # run 开始
        """记录 run 起点。"""
        ctx.started_at = time.perf_counter()  # 重置起点

    async def wrap_model_call(  # 包模型调用
        self,  # 实例
        ctx: RunContext,  # 上下文
        request: ModelRequest,  # 请求
        call_next: ModelCall,  # 下一层
    ) -> ModelResponse:  # 响应
        """计时 + 记录 token；写库由 ctx.trace 适配器负责。"""
        del ctx  # 当前只计时
        started = time.perf_counter()  # 开始
        response = await call_next(request)  # 调模型
        elapsed = int((time.perf_counter() - started) * 1000)  # 耗时
        logger.debug("model {} 耗时 {}ms", request.model, elapsed)  # 调试日志
        return response  # 返回
