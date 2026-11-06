"""官方 AgentMiddleware 实现：取消检查（LangChain 中间件体系）。

框架自带 PII/重试/限额中间件；取消是本项目语境，需自写一个 `AgentMiddleware`。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from langchain.agents.middleware import AgentMiddleware  # 官方中间件基类

from app.agent.context import current_run  # 当前 run（contextvar）


class CancelAgentMiddleware(AgentMiddleware):  # 取消中间件
    """在模型调用前、工具调用前后检查取消信号；不是业务逻辑。"""

    name = "cancel"  # 名称

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # 模型调用前（必须 async：框架 await 此钩子）
        """有活跃 run 且已取消则抛 RunCancelled。"""
        del state, runtime  # 不读
        ctx = current_run()  # 当前 run
        if ctx is not None:  # 有上下文
            ctx.check_cancelled()  # 可能抛
        return None  # 不改状态

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:  # 包工具调用
        """调用前后各查一次取消；工具非流式。"""
        ctx = current_run()  # 当前 run
        if ctx is not None:  # 有上下文
            ctx.check_cancelled()  # 调前
        result = await handler(request)  # 真调用
        if ctx is not None:  # 有上下文
            ctx.check_cancelled()  # 调后
        return result  # 返回