"""契约层：事件协议、命名、id 注册、状态合并规则（后续所有层的公共语言）。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.contracts.events import PAYLOAD_BY_TYPE, EventEnvelope  # 事件信封与载荷映射
from app.agent.contracts.kinds import (  # 枚举、终态与步骤分组
    GROUP_LABELS,  # 分组 → 组名
    GROUP_ORDER,  # 分组展示顺序
    RETRY_KIND_LABELS,  # 重试类别 → 人话
    TERMINAL_TYPES,  # 终态集合
    EventType,  # 事件类型
    StageGroup,  # 步骤分组
    StageStatus,  # 步骤状态
)  # 导入结束
from app.agent.contracts.naming import Namer  # 命名器
from app.agent.contracts.reducers import merge_by_id, merge_usage  # 合并规则
from app.agent.contracts.registry import DuplicateIdError, IdRegistry  # id 注册表
from app.agent.contracts.serialize import from_dict, make, to_dict, validate_sequence  # 序列化

__all__ = [  # 对外导出
    "GROUP_LABELS",  # 分组组名
    "GROUP_ORDER",  # 分组顺序
    "PAYLOAD_BY_TYPE",  # 载荷映射
    "RETRY_KIND_LABELS",  # 重试类别说明
    "TERMINAL_TYPES",  # 终态集合
    "DuplicateIdError",  # 重复 id 异常
    "EventEnvelope",  # 事件信封
    "EventType",  # 事件类型
    "IdRegistry",  # id 注册表
    "Namer",  # 命名器
    "StageGroup",  # 步骤分组
    "StageStatus",  # 步骤状态
    "from_dict",  # 反序列化
    "make",  # 构造
    "merge_by_id",  # 按 id 合并
    "merge_usage",  # 用量合并
    "to_dict",  # 序列化
    "validate_sequence",  # 序列校验
]  # 导出结束
