"""L0–L3/L5 契约：接入上下文 / 预处理 / 意图 / 分诊 / 校验。

约定：
- **交给模型的结构化输出**(IntentDecision / TriageDecision)用 `Field(description=)` + 默认值；
- **内部数据模型**(AccessContext / PreprocessResult / VerifyResult 等)按仓库约定写行尾中文注释。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, Literal  # 类型

from pydantic import BaseModel, Field  # 校验模型

from app.agent.contracts.output import Candidate  # 澄清候选（复用单一来源）

#: 意图大类（无闲聊）。
IntentKind = Literal["query", "action", "clarify"]  # 意图

#: 分诊路径类型（clarify = 需澄清，unsupported = 无能力）。
PathType = Literal["fast_single", "fast_multi", "agentic", "fast_action", "expert_action", "clarify", "unsupported"]  # 路径

#: 会话阶段。
Phase = Literal["start", "middle", "followup"]  # 阶段


class EntityBundle(BaseModel):  # 实体包
    """显式抽取 + 从历史继承的实体（不做指代改写）。"""

    explicit: dict[str, Any] = Field(default_factory=dict)  # 正则抽出的显式实体
    inherited: dict[str, Any] = Field(default_factory=dict)  # 从最近历史补的实体

    def merged(self) -> dict[str, Any]:  # 合并视图
        """显式优先，历史兜底。"""
        return {**self.inherited, **self.explicit}  # 显式覆盖继承


class TimeRange(BaseModel):  # 绝对时间范围
    """相对时间解析结果（零 LLM）。"""

    start: str = ""  # 起始（ISO 或日期）
    end: str = ""  # 结束
    label: str = ""  # 原词（如 上季度）


class AccessContext(BaseModel):  # L0 接入上下文
    """鉴权/会话绑定的产物；当前单用户默认 user_id=37979。"""

    user_id: str = "37979"  # 默认用户
    tenant_id: str = ""  # 租户（暂不做多租户，占位）
    roles: list[str] = Field(default_factory=list)  # 角色
    permissions: frozenset[str] = Field(default_factory=frozenset)  # 权限
    thread_id: str = ""  # 确定性会话标识
    sanitized_query: str = ""  # 脱敏后输入
    pii_counts: dict[str, int] = Field(default_factory=dict)  # PII 类型计数（不存原值）


class PreprocessResult(BaseModel):  # L1 预处理产物
    """规范化后的可检索/可分诊上下文。"""

    query: str = ""  # 规范化 query
    entities: EntityBundle = Field(default_factory=EntityBundle)  # 实体
    time_range: TimeRange | None = None  # 时间范围（解析失败为 None）
    flags: dict[str, Any] = Field(default_factory=dict)  # 硬约束标志
    rewritten: bool = False  # 是否触发过改写
    rewrite_reason: str = ""  # 改写原因
    phase: Phase = "start"  # 会话阶段
    pending_clarify: dict[str, Any] | None = None  # 待接住的澄清


class IntentDecision(BaseModel):  # L2 意图决策（模型结构化输出）
    """三层路由（规则/语义/LLM）的最终意图。"""

    intent: IntentKind = Field(default="clarify", description="query=资料检索/问答; action=执行操作; clarify=需澄清")  # 意图
    domain: str = Field(default="", description="业务域，如 knowledge/order/payment；未知留空")  # 域
    confidence: float = Field(default=0.0, description="置信度 0..1")  # 置信
    source: Literal["rule", "semantic", "llm"] = Field(default="rule", description="谁判的")  # 来源
    reason: str = Field(default="", description="一句路由理由")  # 理由


class TriageDecision(BaseModel):  # L3 分诊决策（模型结构化输出）
    """统一执行计划；条件边直读本对象，不做字段映射。"""

    intent: IntentKind = Field(default="clarify", description="意图大类")  # 意图
    domain: str = Field(default="", description="业务域")  # 域
    path_type: PathType = Field(default="unsupported", description="执行路径；unsupported 表示无对应能力")  # 路径
    agent: str = Field(default="", description="目标专家；path_type 为 agentic/expert_action 时非空")  # 专家
    approval_required: bool = Field(default=False, description="是否需人工审批（HITL）")  # 审批
    missing_entities: list[str] = Field(default_factory=list, description="缺失的必要实体，触发澄清")  # 缺参
    clarify_options: list[Candidate] = Field(default_factory=list, description="澄清候选项")  # 候选
    escalate_reason: str = Field(default="", description="升级/兜底原因")  # 原因
    retry_after_failure: bool = Field(default=False, description="上一轮失败后的重试路径")  # 重试


class VerifyResult(BaseModel):  # L5 引用校验结果
    """答案引用与证据支撑校验的产物。"""

    valid: list[int] = Field(default_factory=list)  # 合法引用序号
    invalid: list[int] = Field(default_factory=list)  # 越界引用序号
    missing: list[int] = Field(default_factory=list)  # 应引未引
    unsupported: list[int] = Field(default_factory=list)  # 无证据支撑的断言
    coverage: float = 0.0  # 覆盖率
    passed: bool = False  # 是否通过
    reasons: list[str] = Field(default_factory=list)  # 未通过原因


__all__ = [  # 对外导出
    "AccessContext",  # 接入上下文
    "EntityBundle",  # 实体包
    "IntentDecision",  # 意图决策
    "IntentKind",  # 意图枚举
    "PathType",  # 路径枚举
    "Phase",  # 阶段枚举
    "PreprocessResult",  # 预处理产物
    "TimeRange",  # 时间范围
    "TriageDecision",  # 分诊决策
    "VerifyResult",  # 校验结果
]