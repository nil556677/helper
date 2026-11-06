"""知识专家（knowledge_agent）：多功能 RAG，检索/资料问答，全部只读。

底层检索唯一源：`search_knowledge_base` 工具 → `app/agent/nodes/rag`。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.agents.base import AgentDescriptor, build_expert_agent  # 基座

DESCRIPTOR = AgentDescriptor(  # 专家描述符
    name="knowledge_agent",  # 功能点：资料检索与问答
    description="知识专家：在本地资料/知识库中检索并据资料回答问题",  # 能力边界（不含风险词）
    tools=("search_knowledge_base",),  # 工具（只读检索）
    side_effect="none",  # 无副作用
    prompt=(  # 系统提示
        "你是知识专家：只用工具在本地资料库检索，并严格依据检索到的资料作答；"
        "回答中的事实必须能在资料中找到支撑，引用用 [编号] 标注；资料不足时说明不足，不要编造。"
    ),
    max_tools=10,  # 工具上限
)  # 描述符结束


def build(model: Any, tools: list[Any]) -> Any:  # 构建专家图
    """按描述符构建 create_agent 图（只读 RAG 工具）。"""
    return build_expert_agent(model, DESCRIPTOR, list(tools))  # 构建