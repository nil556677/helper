"""工具次数限制：超限返回结构化错误，不抛异常穿透图。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.context import RunContext  # 运行上下文
from app.agent.events import tool as tool_event  # 事件
from app.agent.middleware.base import (  # 中间件契约
    Middleware,
    ToolCall,
    ToolCallRequest,
    ToolCallResult,
)

#: 默认限额；后续可移到配置。
MAX_TOTAL = 8  # 每轮总次数
MAX_PER_TOOL = 3  # 单工具次数


class ToolLimitMiddleware(Middleware):  # 工具限额
    """计数只存在 RunContext，不跨轮持久化。"""

    name = "tool_limit"  # 名称

    async def wrap_tool_call(  # 包工具调用
        self,  # 实例
        ctx: RunContext,  # 上下文
        request: ToolCallRequest,  # 请求
        call_next: ToolCall,  # 下一层
    ) -> ToolCallResult:  # 结果
        """超限直接返回错误，模型可继续回答。"""
        total = sum(ctx.tool_counts.values())  # 总次数
        used = ctx.tool_counts.get(request.name, 0)  # 本工具次数
        if total >= MAX_TOTAL or used >= MAX_PER_TOOL:  # 超限
            ctx.emit(tool_event(request.name, "limited"))  # 事件
            return ToolCallResult(  # 结构化错误
                ok=False,  # 失败
                text=f"工具 {request.name} 调用次数已达上限",  # 给模型看
                error_kind="tool_limit",  # 失败类型
            )  # 返回结束
        ctx.tool_counts[request.name] = used + 1  # 先计数
        ctx.emit(tool_event(request.name, "running"))  # 开始事件
        result = await call_next(request)  # 调工具
        ctx.emit(tool_event(request.name, "done" if result.ok else "failed", result.text[:200]))  # 结束事件
        return result  # 返回
