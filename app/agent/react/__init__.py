"""自定义 ReAct 图（agent → approval_gate → tools → agent）。

M3 本版落策略/Limits 与目录骨架；图接线与审批门接入随 M3 正片完成。
"""

from __future__ import annotations  # 延迟值注解

from app.agent.react.policies.approval import should_require_approval  # 审批策略
from app.agent.react.policies.limits import (  # 限额
    MAX_PER_TOOL,  # 单工具次数
    RECURSION_LIMIT,  # 递归上限
    TOKEN_BUDGET_PER_REQUEST,  # token 预算
)  # 导入结束

__all__ = [  # 导出
    "should_require_approval",  # 审批策略
    "MAX_PER_TOOL",  # 单工具次数
    "RECURSION_LIMIT",  # 递归上限
    "TOKEN_BUDGET_PER_REQUEST",  # token 预算
]  # 结束