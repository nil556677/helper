"""状态机导出。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.fsm.machine import InvalidTransition, RunMachine, Transition  # 状态机
from app.agent.fsm.states import TERMINAL_STATES, RunEvent, RunState, StageState  # 状态

__all__ = [  # 导出列表
    "TERMINAL_STATES",  # 终态
    "InvalidTransition",  # 非法迁移
    "RunEvent",  # 迁移事件
    "RunMachine",  # 状态机
    "RunState",  # 运行状态
    "StageState",  # 步骤状态
    "Transition",  # 迁移记录
]  # 列表结束
