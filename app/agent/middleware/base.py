"""中间件协议与链式组合：只做横切，不写业务。"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import AsyncIterator, Awaitable, Callable  # 回调/流类型
from dataclasses import dataclass, field  # 数据类
from typing import Any  # 任意值

from app.agent.context import RunContext  # 运行上下文


@dataclass(frozen=True)
class ModelRequest:  # 一次模型请求
    """中间件可改的请求形状。"""

    model: str  # 模型名
    messages: list[dict[str, Any]]  # 消息列表
    system: str = ""  # 系统提示
    metadata: dict[str, Any] = field(default_factory=dict)  # 附加信息


@dataclass(frozen=True)
class ModelResponse:  # 一次模型响应
    """非流式给 text，流式给 stream；两者至少一个。"""

    text: str = ""  # 完整文本（非流式）
    usage: dict[str, Any] = field(default_factory=dict)  # token 用量
    first_token_ms: int | None = None  # 首 token 耗时
    stream: AsyncIterator[str] | None = None  # token 流


@dataclass(frozen=True)
class ToolCallRequest:  # 一次工具调用请求
    """工具名 + 参数。"""

    name: str  # 工具名
    args: dict[str, Any] = field(default_factory=dict)  # 参数


@dataclass(frozen=True)
class ToolCallResult:  # 工具调用结果
    """成功/失败都用它，避免抛异常穿透图。"""

    ok: bool  # 是否成功
    text: str = ""  # 结果文本
    error_kind: str = ""  # 失败类型


ModelCall = Callable[[ModelRequest], Awaitable[ModelResponse]]  # 模型调用签名
ToolCall = Callable[[ToolCallRequest], Awaitable[ToolCallResult]]  # 工具调用签名


class Middleware:  # 中间件基类
    """默认全部放行；子类只覆写关心的钩子。"""

    name: str = "middleware"  # 名称

    async def before_run(self, ctx: RunContext) -> None:  # run 开始
        """在进入父图前调用。"""
        del ctx  # 默认不读上下文
        return None  # 默认不做

    async def after_run(self, ctx: RunContext) -> None:  # run 结束
        """在父图结束后调用，逆序执行。"""
        del ctx  # 默认不读上下文
        return None  # 默认不做

    async def wrap_model_call(  # 包住模型调用
        self,  # 中间件实例
        ctx: RunContext,  # 运行上下文
        request: ModelRequest,  # 请求
        call_next: ModelCall,  # 下一个中间件/模型
    ) -> ModelResponse:  # 响应
        """默认直接放行。"""
        del ctx  # 默认不读上下文
        return await call_next(request)  # 放行

    async def wrap_tool_call(  # 包住工具调用
        self,  # 中间件实例
        ctx: RunContext,  # 运行上下文
        request: ToolCallRequest,  # 请求
        call_next: ToolCall,  # 下一个中间件/工具
    ) -> ToolCallResult:  # 结果
        """默认直接放行。"""
        del ctx  # 默认不读上下文
        return await call_next(request)  # 放行


class MiddlewareChain:  # 中间件链
    """顺序即洋葱：before 正序、after 逆序、wrap 由外到内。"""

    def __init__(self, middlewares: list[Middleware]) -> None:  # 构造
        self._items = list(middlewares)  # 固定顺序

    async def before_run(self, ctx: RunContext) -> None:  # run 前
        """按注册顺序执行。"""
        for middleware in self._items:  # 正序
            await middleware.before_run(ctx)  # 调用

    async def after_run(self, ctx: RunContext) -> None:  # run 后
        """按注册逆序执行。"""
        for middleware in reversed(self._items):  # 逆序
            await middleware.after_run(ctx)  # 调用

    async def model_call(  # 调模型
        self,  # 链
        ctx: RunContext,  # 上下文
        request: ModelRequest,  # 请求
        call: ModelCall,  # 最内层真实调用
    ) -> ModelResponse:  # 响应
        """组合所有 wrap_model_call。"""
        return await self._run_model(0, ctx, request, call)  # 从最外层开始

    async def _run_model(  # 递归执行模型链
        self,  # 链
        index: int,  # 当前下标
        ctx: RunContext,  # 上下文
        request: ModelRequest,  # 请求
        call: ModelCall,  # 最内层
    ) -> ModelResponse:  # 响应
        """下标越界就调真实模型。"""
        if index >= len(self._items):  # 到底了
            return await call(request)  # 调真实模型
        middleware = self._items[index]  # 当前中间件

        async def call_next(next_request: ModelRequest) -> ModelResponse:  # 下一层
            """把请求交给下一个中间件。"""
            return await self._run_model(index + 1, ctx, next_request, call)  # 递归

        return await middleware.wrap_model_call(ctx, request, call_next)  # 当前层

    async def tool_call(  # 调工具
        self,  # 链
        ctx: RunContext,  # 上下文
        request: ToolCallRequest,  # 请求
        call: ToolCall,  # 最内层真实调用
    ) -> ToolCallResult:  # 结果
        """组合所有 wrap_tool_call。"""
        return await self._run_tool(0, ctx, request, call)  # 从最外层开始

    async def _run_tool(  # 递归执行工具链
        self,  # 链
        index: int,  # 当前下标
        ctx: RunContext,  # 上下文
        request: ToolCallRequest,  # 请求
        call: ToolCall,  # 最内层
    ) -> ToolCallResult:  # 结果
        """下标越界就调真实工具。"""
        if index >= len(self._items):  # 到底了
            return await call(request)  # 调真实工具
        middleware = self._items[index]  # 当前中间件

        async def call_next(next_request: ToolCallRequest) -> ToolCallResult:  # 下一层
            """把请求交给下一个中间件。"""
            return await self._run_tool(index + 1, ctx, next_request, call)  # 递归

        return await middleware.wrap_tool_call(ctx, request, call_next)  # 当前层
