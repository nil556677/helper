"""只读查询专家：知识库检索 / 时间 / 天气，无任何副作用。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.agents.base import AgentDescriptor, build_expert_agent  # 基座

DESCRIPTOR = AgentDescriptor(  # 专家描述符
    name="query_agent",  # 种类
    description="只读查询专家：知识库检索、当前时间、天气查询",  # 能力
    tools=("search_knowledge_base", "current_time", "query_weather"),  # 工具
    side_effect="none",  # 无副作用
    prompt="你是只读查询专家：只用工具做检索与查询，绝不做任何修改或外发操作；完成后给出简洁结果。",  # 提示
    max_tools=10,  # 工具上限
)  # 描述符结束


def build(model: Any, tools: list[Any]) -> Any:  # 构建专家图
    """按描述符构建 create_agent 图（只读工具）。"""
    return build_expert_agent(model, DESCRIPTOR, list(tools))  # 构建