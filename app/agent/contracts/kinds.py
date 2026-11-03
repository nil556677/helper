"""事件类型与步骤状态：契约层的判别字段，UI / SSE / trace 共用。"""

from __future__ import annotations  # 类型注解延迟求值

from enum import StrEnum  # 字符串枚举


class EventType(StrEnum):  # 事件类型
    """前端只认这些 type；新增只加不删，保持向后兼容。"""

    ACK = "ack"  # 已收到
    STATE = "state"  # run 状态迁移
    STAGE = "stage"  # 节点/步骤开始与结束
    BASIS = "basis"  # 检索依据（关键词/命中/来源）
    SETTINGS = "settings"  # 本轮模型/提示词设置
    REASONING = "reasoning"  # 模型思考流
    CONFLICT = "conflict"  # 资料与通用知识冲突
    APPROVAL = "approval"  # 等待用户审批
    CONTEXT = "context"  # 上下文装载/裁剪
    PII = "pii"  # 脱敏统计
    TOOL = "tool"  # 工具调用
    DELTA = "delta"  # 答案增量
    REPLACE = "replace"  # 整段替换（校验不通过时降级为原文）
    CITATION = "citation"  # 引用
    USAGE = "usage"  # token 用量
    NOTE = "note"  # 一句说明（无证据/未标注）
    VERIFY = "verify"  # 引用校验结果（L1/L3）
    DONE = "done"  # 完成
    CANCELLED = "cancelled"  # 已停止
    ERROR = "error"  # 失败
    RETRY = "retry"  # 外部请求重试（第几次 / 什么错误 / 等多久）


TERMINAL_TYPES = frozenset(  # 终态集合
    {EventType.DONE, EventType.CANCELLED, EventType.ERROR}  # 三选一
)  # frozenset 结束


class StageStatus(StrEnum):  # 单个步骤的运行时状态
    """步骤行只认这些状态。"""

    RUNNING = "running"  # 执行中
    DONE = "done"  # 完成
    SKIPPED = "skipped"  # 跳过
    FAILED = "failed"  # 失败


#: 重试错误类别 → 人话（``retry`` 事件的展示用；与 B 的错误分类码一一对应）。
RETRY_KIND_LABELS: dict[str, str] = {  # 类别说明
    "rate_limit": "被限流",  # 429
    "server": "服务端错误",  # 5xx
    "timeout": "请求超时",  # 超时
    "connection": "连接中断",  # 连接重置
    "config": "配置错误",  # 400/401/403/404
    "unknown": "未知错误",  # 兜底
}  # 说明结束


class StageGroup(StrEnum):  # 用户级步骤分组
    """步骤在界面上归到哪一组：**只在 stage() 调用处声明**，不再有「id → 分组」的集中映射表。"""

    PREPARE = "prepare"  # 理解问题（读历史/附件/改写）
    SEARCH = "search"  # 查找资料（召回/融合/回填）
    FILTER = "filter"  # 筛选重点（精排/门限/证据）
    TOOL = "tool"  # 使用工具
    GENERATE = "generate"  # 生成回答


#: 单一事实来源：一行同时给定「展示顺序 + 中文组名 + 图标」，GROUP_ORDER / GROUP_LABELS /
#: GROUP_ICONS 都由它派生，不手动重复（顺序/文案/图标不会互相不一致）。
_GROUPS: tuple[tuple[StageGroup, str, str], ...] = (  # （分组, 组名, 图标）
    (StageGroup.PREPARE, "理解问题", "psychology"),  # 先理解（思考）
    (StageGroup.SEARCH, "查找资料", "search"),  # 再查找（搜索）
    (StageGroup.FILTER, "筛选重点", "filter_alt"),  # 再筛选（过滤/精排）
    (StageGroup.TOOL, "使用工具", "handyman"),  # 工具调用（工具）
    (StageGroup.GENERATE, "生成回答", "auto_awesome"),  # 最后生成（AI 生成）
)  # 清单结束


#: 分组的展示顺序（按 _GROUPS 声明序派生，不重复手列）。
GROUP_ORDER: tuple[StageGroup, ...] = tuple(group for group, _, _ in _GROUPS)  # 顺序

#: 分组 → 用户看到的组名（同样从 _GROUPS 派生，保证与顺序不会不一致）。
GROUP_LABELS: dict[StageGroup, str] = {group: label for group, label, _ in _GROUPS}  # 标签

#: 分组 → 图标（quasar/material 名）；与顺序/组名同源，UI 渲染步骤/分组时消费它。
GROUP_ICONS: dict[StageGroup, str] = {group: icon for group, _, icon in _GROUPS}  # 图标
