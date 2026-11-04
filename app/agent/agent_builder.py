"""答题 agent 的官方构造：`create_agent` + 官方中间件栈（框架优先，单一入口）。

把「模型 + 工具 + 横切」收成一处：PII（官方）、重试（官方，由调用方决定是否叠加）、
轮次上限（官方）、取消（自写）。追踪由 `TraceCallbacks` 在调用 config 上统一挂载。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from langchain.agents import create_agent  # 官方 agent 工厂
from langchain.agents.middleware import (  # 官方中间件
    AgentMiddleware,  # 类型
    ModelCallLimitMiddleware,  # 轮次上限
    PIIMiddleware,  # 脱敏
)

from app.agent.middleware.agent_mw import CancelAgentMiddleware  # 取消


def default_middleware(*, run_limit: int = 6, extra: list[AgentMiddleware] | None = None) -> list[AgentMiddleware]:  # 默认栈
    """固定顺序：取消 → PII（入/出/工具结果）→ 轮次上限。"""
    stack: list[AgentMiddleware] = [  # 栈
        CancelAgentMiddleware(),  # 取消尽早短路
        PIIMiddleware("email", strategy="redact", apply_to_input=True, apply_to_output=True, apply_to_tool_results=True),  # 邮箱
        PIIMiddleware("credit_card", strategy="redact", apply_to_input=True, apply_to_output=True, apply_to_tool_results=True),  # 卡号
        ModelCallLimitMiddleware(run_limit=run_limit),  # 工具循环上限
    ]  # 栈结束
    if extra:  # 额外中间件
        stack.extend(extra)  # 追加
    return stack  # 返回


def build_react_agent(  # 构造答题 agent
    model: Any,  # 已实例化的模型
    tools: list[Any],  # 工具
    system_prompt: str,  # 系统提示
    *,
    run_limit: int = 6,  # 轮次上限
    extra_middleware: list[AgentMiddleware] | None = None,  # 额外中间件
    name: str = "agent",  # 名称
) -> Any:  # 编译后的图
    """一次构造；调用方负责 `ainvoke(..., config={"callbacks": [TraceCallbacks()]})`。"""
    return create_agent(  # 官方工厂
        model=model,  # 模型
        tools=tools,  # 工具
        system_prompt=system_prompt,  # 系统提示
        middleware=default_middleware(run_limit=run_limit, extra=extra_middleware),  # 横切栈
        name=name,  # 名称
    )  # 返回图