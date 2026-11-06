"""契约层：事件协议、命名、id 注册、状态合并规则（后续所有层的公共语言）。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.contracts.collab import (  # 任务委派契约
    TASK_TRANSITIONS,  # 任务迁移
    TERMINAL_TASK_STATUSES,  # 任务终态
    AgentCard,  # Agent 卡
    TaskEnvelope,  # 任务信封
    TaskStatus,  # 任务状态
    is_final,  # 任务终态判断
    task_can_transition,  # 迁移判断
)  # 委派导入结束
from app.agent.contracts.events import PAYLOAD_BY_TYPE, EventEnvelope  # 事件信封与载荷映射
from app.agent.contracts.kinds import (  # 枚举、终态与步骤分组
    GROUP_ICONS,  # 分组图标
    GROUP_LABELS,  # 分组 → 组名
    GROUP_ORDER,  # 分组展示顺序
    RETRY_KIND_LABELS,  # 重试类别 → 人话
    TERMINAL_TYPES,  # 终态集合
    EventType,  # 事件类型
    StageGroup,  # 步骤分组
    StageStatus,  # 步骤状态
)  # 导入结束
from app.agent.contracts.naming import Namer  # 命名器

# ---- 新契约域（集中定义；域内单向依赖：kinds ← events ← serialize，其余互不依赖）----
from app.agent.contracts.output import (  # 模型输出契约
    Candidate,  # 澄清候选
    DecisionTarget,  # 决策目标枚举
    RouteDecision,  # 决策对象
    candidate_id,  # 候选 id 生成
)  # 输出导入结束
from app.agent.contracts.reducers import merge_by_id, merge_usage  # 合并规则
from app.agent.contracts.registry import DuplicateIdError, IdRegistry  # id 注册表
from app.agent.contracts.routing import (  # 意图路由契约
    AGENT_KINDS,  # 专家种类（唯一枚举源）
    SIDE_EFFECTS,  # 副作用等级
    AgentDescriptor,  # 专家描述符
    ClarifyOptions,  # 澄清选项
    InterruptPayload,  # HITL 负载
    ResumeCommand,  # HITL 恢复
    RouteCandidate,  # 语义候选
    RouteSpec,  # 固定路径
    RuleHit,  # 规则命中
    SemanticRecall,  # 语义召回
)
from app.agent.contracts.triage import (  # L0–L3/L5 契约
    AccessContext,  # 接入上下文
    EntityBundle,  # 实体包
    IntentDecision,  # 意图决策
    PreprocessResult,  # 预处理产物
    TimeRange,  # 时间范围
    TriageDecision,  # 分诊决策
    VerifyResult,  # 校验结果
)
from app.agent.contracts.serialize import from_dict, make, to_dict, validate_sequence  # 序列化
from app.agent.contracts.state import (  # 共享状态契约
    WRITE_TABLE,  # 写契约表
    ResultLike,  # 结果形状协议（只依赖形状，不 import core）
    Turn,  # 轮次
    TurnStatus,  # 轮次状态
    TurnToolCall,  # 工具审计
    WriteRule,  # 写规则
    merge_turns,  # 轮次合并
    only_done,  # 只喂 done
)  # 状态导入结束
from app.agent.contracts.tools import (  # 工具契约
    ErrorCode,
    Mutation,
    ToolDescriptor,
)

__all__ = [  # 对外导出
    "AGENT_KINDS",  # 专家种类
    "GROUP_ICONS",  # 分组图标
    "GROUP_LABELS",  # 分组组名
    "GROUP_ORDER",  # 分组顺序
    "PAYLOAD_BY_TYPE",  # 载荷映射
    "RETRY_KIND_LABELS",  # 重试类别说明
    "SIDE_EFFECTS",  # 副作用等级
    "TASK_TRANSITIONS",  # 任务迁移
    "TERMINAL_TASK_STATUSES",  # 任务终态
    "TERMINAL_TYPES",  # 终态集合
    "WRITE_TABLE",  # 写契约表
    "AgentCard",  # Agent 卡
    "AgentDescriptor",  # 专家描述符
    "Candidate",  # 澄清候选
    "ClarifyOptions",  # 澄清选项
    "DecisionTarget",  # 决策目标枚举
    "DuplicateIdError",  # 重复 id 异常
    "ErrorCode",  # 错误码常量
    "EventEnvelope",  # 事件信封
    "EventType",  # 事件类型
    "IdRegistry",  # id 注册表
    "InterruptPayload",  # HITL 负载
    "Mutation",  # 变更画像
    "Namer",  # 命名器
    "ResultLike",  # 结果形状协议
    "ResumeCommand",  # HITL 恢复
    "RouteCandidate",  # 语义候选
    "RouteDecision",  # 决策对象
    "RouteSpec",  # 固定路径
    "RuleHit",  # 规则命中
    "SemanticRecall",  # 语义召回
    "StageGroup",  # 步骤分组
    "StageStatus",  # 步骤状态
    "TaskEnvelope",  # 任务信封
    "TaskStatus",  # 任务状态
    "ToolDescriptor",  # 工具描述符
    "Turn",  # 轮次
    "TurnStatus",  # 轮次状态
    "TurnToolCall",  # 工具审计
    "WriteRule",  # 写规则
    "candidate_id",  # 候选 id 生成
    "from_dict",  # 反序列化
    "is_final",  # 任务终态判断
    "make",  # 构造
    "merge_by_id",  # 按 id 合并
    "merge_turns",  # 轮次合并
    "merge_usage",  # 用量合并
    "only_done",  # 只喂 done
    "task_can_transition",  # 任务迁移判断
    "to_dict",  # 序列化
    "validate_sequence",  # 序列校验
]  # 导出结束
