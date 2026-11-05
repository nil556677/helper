"""可逆写专家：建单/改单等可撤销操作（副作用需审批策略，当前无可用工具）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.agents.base import AgentDescriptor, build_expert_agent  # 基座

DESCRIPTOR = AgentDescriptor(  # 专家描述符
    name="action_agent",  # 种类
    description="可逆写专家：创建/修改工单等可撤销操作",  # 能力
    tools=(),  # 当前没有可逆写工具（后续接入）
    side_effect="reversible",  # 可逆
    prompt="你是可逆写专家：只执行可撤销的写操作，严格按给定步骤完成。",  # 提示
    max_tools=10,  # 工具上限
)  # 描述符结束


def build(model: Any, tools: list[Any]) -> Any:  # 构建专家图
    """按描述符构建 create_agent 图（当前无工具，保留接缝）。"""
    return build_expert_agent(model, DESCRIPTOR, list(tools))  # 构建