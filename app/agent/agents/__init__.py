"""多 Agent 层：注册表、任务委派、任务看板投影。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.agents.delegate import (  # 委派
    DelegationResult,  # 结果
    Executor,  # 执行器类型
    delegate,  # 单委派
    delegate_many,  # 并行委派
)  # 导入结束
from app.agent.agents.projection import task_board  # 看板
from app.agent.agents.registry import AgentRegistry, DuplicateAgentError  # 注册表

__all__ = [  # 对外导出
    "AgentRegistry",  # 注册表
    "DelegationResult",  # 委派结果
    "DuplicateAgentError",  # 重复 agent
    "Executor",  # 执行器
    "delegate",  # 单委派
    "delegate_many",  # 并行委派
    "task_board",  # 任务看板
]  # 导出结束
