"""专家代理基座：描述符约定 + 通用 ReAct 构建（自动发现共用）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.agent_builder import build_react_agent  # 官方 create_agent 构造
from app.agent.contracts.routing import AgentDescriptor  # 描述符（单一来源）

__all__ = ["AgentDescriptor", "build_expert_agent"]  # 导出


def build_expert_agent(  # 按描述符构建一个专家
    model: Any,  # 模型实例
    descriptor: AgentDescriptor,  # 描述符
    tools: list[Any],  # 已按描述符筛选的工具
) -> Any:  # 编译后的 agent 图
    """统一构建：系统提示用描述符的 prompt，轮次上限用 max_tools 收紧。"""
    return build_react_agent(  # 官方构造
        model=model,  # 模型
        tools=tools,  # 工具
        system_prompt=descriptor.prompt,  # 提示
        run_limit=max(1, int(descriptor.max_tools)),  # 轮次上限（工具越少越收紧）
        name=descriptor.name,  # 名称
    )