"""取消中间件：在模型/工具调用前后检查取消信号。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.context import RunContext  # 运行上下文
from app.agent.middleware.base import (  # 中间件契约
    Middleware,
    ModelCall,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolCallRequest,
    ToolCallResult,
)
from app.agent.streaming import aclose  # 关闭上游流


class CancelMiddleware(Middleware):  # 取消检查
    """不做业务，只把 RunCancelled 尽早抛出。"""

    name = "cancel"  # 名称

    async def before_run(self, ctx: RunContext) -> None:  # run 前
        """开始前如果已经请求取消，直接退出。"""
        ctx.check_cancelled()  # 可能抛 RunCancelled

    async def wrap_model_call(  # 包模型调用
        self,  # 实例
        ctx: RunContext,  # 上下文
        request: ModelRequest,  # 请求
        call_next: ModelCall,  # 下一层
    ) -> ModelResponse:  # 响应
        """调用前后 + 流式每个 chunk 都查一次。"""
        ctx.check_cancelled()  # 调前
        response = await call_next(request)  # 调模型
        ctx.check_cancelled()  # 调后
        if response.stream is None:  # 非流式
            return response  # 直接返回

        async def checked_stream():  # 包一层流
            """每个 token 前检查取消；退出时把关闭传到上游。"""
            try:  # 正常/异常都要关
                async for chunk in response.stream:  # 逐 token
                    ctx.check_cancelled()  # 检查
                    yield chunk  # 放行
            finally:  # 取消或客户端断开
                await aclose(response.stream)  # 关掉上游，别让它继续生成

        from dataclasses import replace  # 局部导入避免污染

        return replace(response, stream=checked_stream())  # 替换流

    async def wrap_tool_call(  # 包工具调用
        self,  # 实例
        ctx: RunContext,  # 上下文
        request: ToolCallRequest,  # 请求
        call_next: ToolCall,  # 下一层
    ) -> ToolCallResult:  # 结果
        """调用前后各查一次。"""
        ctx.check_cancelled()  # 调前
        result = await call_next(request)  # 调工具
        ctx.check_cancelled()  # 调后
        return result  # 返回
