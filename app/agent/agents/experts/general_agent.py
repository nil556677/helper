"""通用兜底专家（general_agent）：纯 LLM 节点，无工具、不检索、不闲聊。

只做"能力范围外/无匹配"时的诚实引导：说明系统能做什么、引导表达需求。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.agents.base import AgentDescriptor, build_expert_agent  # 基座

DESCRIPTOR = AgentDescriptor(  # 专家描述符
    name="general_agent",  # 功能点：能力外兜底引导
    description="通用兜底：无匹配专家时，说明系统能力并引导用户表达可处理的需求",  # 能力边界
    tools=(),  # 不挂任何工具
    side_effect="none",  # 无副作用
    prompt=(  # 系统提示
        "你是通用兜底引导：不要闲聊。若问题超出系统能力，如实说明并引导用户换成"
        "资料问答类问题；不要假装拥有数据或工具能力。"
    ),
    max_tools=0,  # 不挂工具
)  # 描述符结束


def build(model: Any, tools: list[Any]) -> Any:  # 构建专家图
    """构建无工具的单轮 LLM 图（仍是统一接口，返回 messages）。"""
    return build_expert_agent(model, DESCRIPTOR, list(tools))  # 构建