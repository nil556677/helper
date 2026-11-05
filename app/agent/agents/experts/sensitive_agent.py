"""不可逆专家：退款/删除/外发通知等（必须 HITL 审批，当前无可用工具）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.agents.base import AgentDescriptor, build_expert_agent  # 基座

DESCRIPTOR = AgentDescriptor(  # 专家描述符
    name="sensitive_agent",  # 种类
    description="不可逆专家：退款/删除/发送外部通知等不可撤销操作",  # 能力
    tools=(),  # 当前没有不可逆工具（后续接入）
    side_effect="irreversible",  # 不可逆 → 必须 HITL
    prompt="你是不可逆操作专家：执行前必须确认已获人工批准；只做被批准的那一步。",  # 提示
    max_tools=10,  # 工具上限
)  # 描述符结束


def build(model: Any, tools: list[Any]) -> Any:  # 构建专家图
    """按描述符构建 create_agent 图（当前无工具，保留接缝）。"""
    return build_expert_agent(model, DESCRIPTOR, list(tools))  # 构建