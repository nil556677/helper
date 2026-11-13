"""中间件协议与链式组合：只做横切，不写业务。"""

from __future__ import annotations  # 类型注解延迟求值

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


class Middleware:  # 中间件基类
    """默认全部放行；子类只覆写关心的钩子。

    只承载 **run 生命周期**钩子（before_run / after_run）。模型/工具调用由框架的
    AgentMiddleware 处理（见 `app.agent.agent_builder`），本基类不再定义逐次调用的
    `wrap_model_call`——那套"自研包裹链"从未接线，是误导性死代码，已移除。
    """

    name: str = "middleware"  # 名称

    async def before_run(self, ctx: RunContext) -> None:  # run 开始
        """在进入父图前调用。"""
        del ctx  # 默认不读上下文
        return None  # 默认不做

    async def after_run(self, ctx: RunContext) -> None:  # run 结束
        """在父图结束后调用，逆序执行。"""
        del ctx  # 默认不读上下文
        return None  # 默认不做


class MiddlewareChain:  # 中间件链
    """顺序即洋葱：before 正序、after 逆序（**只挂 run 生命周期钩子**）。

    模型/工具调用一律由框架的 AgentMiddleware（见 `app.agent.agent_builder`）处理，
    本链不承载逐次调用包裹——那是"自研中间件没接线"的误导性死代码，已移除。
    """

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
