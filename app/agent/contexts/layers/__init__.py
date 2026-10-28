"""分层渲染函数：每个数据块一个纯函数（固定 / 固定变化 / 动态三层）。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.contexts.layers.dynamic_tier import attachments, question, tool_results  # 动态层
from app.agent.contexts.layers.memory_tier import facts, recent, summary  # 固定变化层
from app.agent.contexts.layers.system_tier import agent_rules, static_env, system_base  # 固定层

__all__ = [  # 对外导出（按拼接顺序）
    "agent_rules",  # S2
    "attachments",  # V2
    "facts",  # F1
    "question",  # V3
    "recent",  # F3
    "static_env",  # S3
    "summary",  # F2
    "system_base",  # S1
    "tool_results",  # V1
]
