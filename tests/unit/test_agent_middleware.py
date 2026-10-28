"""中间件链：顺序、脱敏、工具限额、取消。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 事件循环

import pytest  # 测试框架

from app.agent.context import RunCancelled, RunContext  # 上下文
from app.agent.middleware import (  # 中间件
    CancelMiddleware,
    Middleware,
    MiddlewareChain,
    ModelRequest,
    ModelResponse,
    PIIMiddleware,
    ToolCallRequest,
    ToolCallResult,
    ToolLimitMiddleware,
)


def _ctx() -> RunContext:  # 造上下文
    """不接 UI，事件丢弃。"""
    return RunContext(run_id="t1", question="你好")  # 新上下文


def test_chain_order() -> None:  # 链顺序
    """before 正序、wrap 洋葱、after 逆序。"""
    events: list[str] = []  # 记录

    class First(Middleware):  # 外层
        name = "first"  # 名称

        async def before_run(self, ctx: RunContext) -> None:  # run 前
            events.append("first.before")  # 记录

        async def after_run(self, ctx: RunContext) -> None:  # run 后
            events.append("first.after")  # 记录

        async def wrap_model_call(self, ctx, request, call_next):  # 包模型
            events.append("first.in")  # 进
            response = await call_next(request)  # 下一层
            events.append("first.out")  # 出
            return response  # 返回

    class Second(First):  # 内层
        name = "second"  # 名称

        async def before_run(self, ctx: RunContext) -> None:  # run 前
            events.append("second.before")  # 记录

        async def after_run(self, ctx: RunContext) -> None:  # run 后
            events.append("second.after")  # 记录

        async def wrap_model_call(self, ctx, request, call_next):  # 包模型
            events.append("second.in")  # 进
            response = await call_next(request)  # 最内层
            events.append("second.out")  # 出
            return response  # 返回

    chain = MiddlewareChain([First(), Second()])  # 链
    ctx = _ctx()  # 上下文

    async def call(request: ModelRequest) -> ModelResponse:  # 假模型
        events.append("model")  # 记录
        return ModelResponse(text="ok")  # 响应

    async def run() -> None:  # 异步跑
        await chain.before_run(ctx)  # run 前
        await chain.model_call(ctx, ModelRequest(model="m", messages=[]), call)  # 模型
        await chain.after_run(ctx)  # run 后

    asyncio.run(run())  # 执行
    assert events == [  # 顺序断言
        "first.before",  # 正序
        "second.before",  # 正序
        "first.in",  # 外进
        "second.in",  # 内进
        "model",  # 真实调用
        "second.out",  # 内出
        "first.out",  # 外出
        "second.after",  # 逆序
        "first.after",  # 逆序
    ]  # 断言结束


def test_pii_redacts() -> None:  # 脱敏
    """手机号/邮箱不原样出站。"""
    seen: list[str] = []  # 记录发给模型的文本

    async def call(request: ModelRequest) -> ModelResponse:  # 假模型
        seen.extend(str(item.get("content")) for item in request.messages)  # 记录
        return ModelResponse(text="联系 13800138000")  # 响应也带 PII

    async def run() -> ModelResponse:  # 异步跑
        chain = MiddlewareChain([PIIMiddleware()])  # 只有脱敏
        request = ModelRequest(  # 请求
            model="m",  # 模型
            messages=[{"role": "user", "content": "我的手机 13800138000"}],  # 消息
        )  # 请求结束
        return await chain.model_call(_ctx(), request, call)  # 调链

    response = asyncio.run(run())  # 执行
    assert "13800138000" not in seen[0]  # 入站已脱敏
    assert "13800138000" not in response.text  # 出站已脱敏
    assert "REDACTED:phone" in seen[0]  # 有标记


def test_tool_limit() -> None:  # 工具限额
    """第 4 次调用返回 tool_limit。"""
    async def call(request: ToolCallRequest) -> ToolCallResult:  # 假工具
        return ToolCallResult(ok=True, text="ok")  # 成功

    async def run() -> list[ToolCallResult]:  # 异步跑
        chain = MiddlewareChain([ToolLimitMiddleware()])  # 只有限额
        ctx = _ctx()  # 上下文
        return [await chain.tool_call(ctx, ToolCallRequest(name="weather"), call) for _ in range(4)]  # 四次

    results = asyncio.run(run())  # 执行
    assert all(item.ok for item in results[:3])  # 前三次成功
    assert results[3].ok is False  # 第四次失败
    assert results[3].error_kind == "tool_limit"  # 错误类型


def test_cancel() -> None:  # 取消
    """取消后模型调用抛 RunCancelled。"""
    async def call(request: ModelRequest) -> ModelResponse:  # 假模型
        return ModelResponse(text="ok")  # 不应走到

    async def run() -> None:  # 异步跑
        ctx = _ctx()  # 上下文
        ctx.request_cancel()  # 请求取消
        chain = MiddlewareChain([CancelMiddleware()])  # 只有取消
        await chain.model_call(ctx, ModelRequest(model="m", messages=[]), call)  # 应抛

    with pytest.raises(RunCancelled):  # 期望异常
        asyncio.run(run())  # 执行


def test_context_window_is_not_a_middleware_anymore() -> None:  # 收口校验
    """裁剪已归上下文层；中间件包不再导出 context_window。"""
    import app.agent.middleware as mw  # 中间件包

    assert not hasattr(mw, "ContextWindowMiddleware")  # 旧的已移除

