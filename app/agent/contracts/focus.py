"""焦点管理契约：FocusEntity / OrderedGroup / FocusStack / Slots / ResolutionResult / LLM 输出。

- 内部数据模型（FocusEntity/FocusStack/Slots/ResolutionResult/OrderedGroup）按仓库约定写行尾中文注释；
- **发给 LLM 的** `ReferenceAndEntityOutput`（指代消解/意图分类那一趟附带抽取）字段只用
  `Field(description=中文)` + **默认值**（模型才看得见）。
- 都进 `ChatState`（显式 channel），随 checkpointer 跨轮持久化。
"""

from __future__ import annotations  # 延迟值注解

from typing import Literal  # 类型

from pydantic import BaseModel, Field  # 校验

__all__ = [  # 导出
    "ENTITY_TYPES",  # 实体类型
    "ExtractedEntity",  # LLM 附带抽取实体
    "FocusEntity",  # 焦点实体
    "FocusStack",  # 焦点栈
    "OrderedGroup",  # 有序列表
    "ReferenceAndEntityOutput",  # LLM 指代消解输出
    "ResolutionResult",  # 消解结果
    "SlotValue",  # 槽位值
    "Slots",  # 槽位
]

#: 实体类型。
ENTITY_TYPES = ("order_id", "phone", "tracking_no", "policy_term")  # 类型

#: 实体来源。
_SOURCE = Literal["structured", "regex", "lexicon", "llm", "inherited"]  # 来源


class FocusEntity(BaseModel):  # 焦点实体
    entity_type: str = ""  # 实体类型（order_id/phone/tracking_no/policy_term）
    entity_id: str = ""  # 规范化后的值（如 "12345"）
    surface: str = ""  # 原文表面形式
    topic: str = ""  # 所属话题域（order/logistics/refund/policy）
    introduced_at: int = 0  # 引入轮次
    last_mentioned_at: int = 0  # 最后一次提及轮次
    mention_count: int = 0  # 提及次数
    weight: float = 0.0  # 时间衰减权重（候选排序用）
    confidence: float = 0.0  # 抽取置信度 0–1
    source: str = "regex"  # 来源（structured/regex/lexicon/llm/inherited）
    span: tuple[int, int] | None = None  # 原文字符区间（审计/纠正用）


class OrderedGroup(BaseModel):  # 有序列表（序数指代用）
    group_id: str = ""  # 列表 id
    label: str = ""  # 人话标签（"订单列表"/"退款选项"）
    items: list[FocusEntity] = Field(default_factory=list)  # 按用户列举顺序的实体
    established_at: int = 0  # 建立轮次
    original_intent: str = ""  # 建立时的意图（"第二个呢"继承）
    source: str = "user_enumeration"  # user_enumeration/system_options/tool_result
    topic: str = ""  # 话题域


class FocusStack(BaseModel):  # 焦点栈
    stack: list[FocusEntity] = Field(default_factory=list)  # 单实体
    groups: list[OrderedGroup] = Field(default_factory=list)  # 有序列表（最新在前）
    current_topic: str = ""  # 当前话题域
    turn_index: int = 0  # 当前轮次


class SlotValue(BaseModel):  # 槽位值
    value: str | None = None  # 值
    filled_at: int | None = None  # 填充轮次
    source: str = "user_explicit"  # user_explicit/inherited/default


class Slots(BaseModel):  # 槽位（按参数类型组织）
    values: dict[str, SlotValue] = Field(default_factory=dict)  # order_id/phone/...


class ResolutionResult(BaseModel):  # 消解结果（内部）
    bound: FocusEntity | None = None  # 绑定的实体（无则 None）
    source: str = ""  # explicit/unique/proximal/recent/weighted/ordinal/inherited
    ambiguous: bool = False  # 是否歧义
    candidates: list[FocusEntity] = Field(default_factory=list)  # 候选（歧义时前 N 个）
    reason: str = ""  # 歧义原因（no_candidate/no_valid_group/ordinal_out_of_range/...）
    inherited_intent: str | None = None  # 序数指代时继承的意图


class ExtractedEntity(BaseModel):  # LLM 附带抽取的实体（发给模型）
    entity_type: str = Field(default="", description="实体类型：order_id/phone/tracking_no/policy_term")  # 类型
    entity_id: str = Field(default="", description="规范化后的值")  # 值
    confidence: float = Field(default=0.0, description="抽取置信度 0..1，低于 0.5 不入栈")  # 置信


class ReferenceAndEntityOutput(BaseModel):  # LLM 指代消解 + 附带抽取（发给模型）
    bound_entity_id: str = Field(default="", description="绑定的实体 id；无法确定留空")  # 绑定
    bound_group_id: str = Field(default="", description="绑定的有序列表 id；序数时用")  # 有序列表
    ordinal_index: int | None = Field(default=None, description="序数索引（0 起；-1 表示最后一个）")  # 序数
    extra_entities: list[ExtractedEntity] = Field(default_factory=list, description="顺带抽取的实体")  # 附带抽取
    ambiguous: bool = Field(default=False, description="是否因指代歧义需要澄清")  # 歧义
    clarification_question: str | None = Field(default=None, description="歧义时要问用户的话")  # 澄清问句
