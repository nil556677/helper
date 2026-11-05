"""任务委派契约（A2A）：跨 Agent 外包任务与 Agent 卡（多 Agent 阶段用）。"""

from __future__ import annotations  # 类型注解延迟求值

import time  # 默认时间戳
from enum import StrEnum  # 字符串枚举
from typing import Any  # 任意值

from pydantic import BaseModel, Field, model_validator  # 校验模型


class TaskStatus(StrEnum):  # 任务状态
    SUBMITTED = "submitted"  # 已提交
    WORKING = "working"  # 执行中
    INPUT_REQUIRED = "input_required"  # 等补参
    AUTH_REQUIRED = "auth_required"  # 等授权
    COMPLETED = "completed"  # 完成
    FAILED = "failed"  # 失败
    CANCELED = "canceled"  # 取消
    REJECTED = "rejected"  # 拒绝


TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(  # 终态（吸收态）
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED, TaskStatus.REJECTED}  # 四个终态
)  # frozenset 结束

#: 允许的迁移（单向；终态为吸收态，不再接受消息）。
TASK_TRANSITIONS: frozenset[tuple[TaskStatus, TaskStatus]] = frozenset({  # 迁移集合
    (TaskStatus.SUBMITTED, TaskStatus.WORKING),  # 开始执行
    (TaskStatus.SUBMITTED, TaskStatus.REJECTED),  # 被拒
    (TaskStatus.SUBMITTED, TaskStatus.CANCELED),  # 提交后取消
    (TaskStatus.WORKING, TaskStatus.INPUT_REQUIRED),  # 需补参
    (TaskStatus.WORKING, TaskStatus.AUTH_REQUIRED),  # 需授权
    (TaskStatus.WORKING, TaskStatus.COMPLETED),  # 完成
    (TaskStatus.WORKING, TaskStatus.FAILED),  # 失败
    (TaskStatus.WORKING, TaskStatus.CANCELED),  # 执行中取消
    (TaskStatus.INPUT_REQUIRED, TaskStatus.WORKING),  # 补参后恢复
    (TaskStatus.AUTH_REQUIRED, TaskStatus.WORKING),  # 授权后恢复
    (TaskStatus.INPUT_REQUIRED, TaskStatus.CANCELED),  # 补参中取消
    (TaskStatus.AUTH_REQUIRED, TaskStatus.CANCELED),  # 授权中取消
})  # 集合结束


def task_can_transition(status: TaskStatus, target: TaskStatus) -> bool:  # 能否迁移
    """终态为吸收态：只在目标等于原状时返回 True。"""
    if status in TERMINAL_TASK_STATUSES:  # 已在终态
        return status is target  # 只能停在原状
    return (status, target) in TASK_TRANSITIONS  # 查表


def is_final(status: TaskStatus) -> bool:  # 是否终态
    return status in TERMINAL_TASK_STATUSES  # 归属判断


class AgentCard(BaseModel):  # Agent 能力声明（内存注册，不建表）
    """只声明能力 / 工具 ID / 权限 / 协议版本，不暴露实现。"""

    agent_id: str  # 唯一 id
    name: str = ""  # 显示名
    version: int = 1  # 版本
    capabilities: list[str] = Field(default_factory=list)  # 能力描述
    tools: list[str] = Field(default_factory=list)  # 工具 id 列表（字符串）
    permissions: tuple[str, ...] = Field(default_factory=tuple)  # 权限
    protocol_version: int = 1  # 协议版本


class TaskEnvelope(BaseModel):  # 一次外包任务
    """task_id 由目标 Agent 服务端生成；状态由服务端记录。"""

    task_id: str  # 服务端生成，客户端不自造
    idempotency_key: str  # 幂等键（发起方生成；必填，下游据此去重）
    initiator: str = ""  # 发起方 agent id
    target: str = ""  # 目标 agent id
    input: Any = None  # 输入数据
    output: Any = None  # 目标 Agent 完成后的结果
    error: str = ""  # FAILED 时的人话原因
    deadline: float | None = None  # 截止时间
    status: TaskStatus = TaskStatus.SUBMITTED  # 服务端记录
    trace_id: str = ""  # 关联追踪
    created_at: float = Field(default_factory=time.time)  # 创建时间
    updated_at: float = Field(default_factory=time.time)  # 更新时间

    @model_validator(mode="after")  # 跨字段校验
    def _check_deadline(self) -> TaskEnvelope:  # 截止时间校验
        """deadline 非空时必须严格晚于 created_at，避免构造出已过期的任务。"""
        if self.deadline is not None and self.deadline <= self.created_at:  # 截止不晚于创建
            raise ValueError("deadline 必须大于 created_at")  # 拒绝
        return self  # 通过
