"""LangGraph 的状态（只放可序列化业务值）。

边界铁律：**业务数据只进 ChatState；进程内副作用（emit/取消/状态机/计时/步骤累积）只进
RunContext**。本文件不定义 reducer（steps 过程累积在 RunContext，不用图 channel）。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, TypedDict  # 状态类型工具


class ChatState(TypedDict, total=False):  # 图状态（字段都可缺省）
    # 输入
    question: str  # 用户问题
    attachments: list[dict[str, Any]]  # 本轮附件（元数据，正文不入 state）
    conversation_id: str  # 会话 id
    summary: str  # 滚动摘要（load_context 写、装配读）

    # 多轮
    turns: list[dict[str, Any]]  # 对话历史（Turn 的 JSON 形态；成对、只喂 done）

    # 路由（唯一决策对象）
    route: dict[str, Any]  # RouteDecision 的 JSON 形态；迁移与边路由都读它
    preprocessed: dict[str, Any]  # PreprocessResult 的 JSON 形态（L1 产物）
    intent: dict[str, Any]  # IntentDecision 的 JSON 形态（L2 产物）
    flags: dict[str, Any]  # 硬约束标志（L3 短路读）
    pending_clarify: dict[str, Any]  # 上轮待澄清（下一轮 phase=followup）
    rule_hits: list[dict[str, Any]]  # 规则层命中明细（可观测）
    semantic_recall: dict[str, Any]  # SemanticRecall 的 JSON 形态（语义层只写）

    # 澄清（semantic_miss 出口，收敛闭环）
    clarify_options: dict[str, Any]  # ClarifyOptions 的 JSON 形态
    clarify_attempted: bool  # 是否尝试过澄清（限轮次用）
    clarify_attempt: int  # 本会话第几次澄清

    # 兜底子图结果（父图黑盒返回）
    fallback_used: bool  # 本次请求最多进兜底子图一次
    final_response: str  # 兜底子图返回的最终回答（并入 answer 用）

    # 检索
    hits: list[dict[str, Any]]  # 召回命中
    evidence: list[dict[str, Any]]  # 证据片段

    # 答复
    answer: str  # 回答正文
    note: str  # 一句说明
    note_kind: str  # 说明样式（plain 普通 / error 错误）
    fragments: list[dict[str, Any]]  # 引用片段
    usage: dict[str, Any]  # token 用量（可序列化）

    # 元数据（对账 / checkpoint 定位）
    turn_id: str  # 逻辑轮次 id（= user 消息 id）
    status: str  # 运行状态（running/done/cancelled/failed）
    attempt: int  # 第几次尝试

    # 可观测 / 诊断（checkpoint 也序列化，恢复时可查）
    settings: dict[str, Any]  # 本轮设置
    basis: dict[str, Any]  # 检索依据
    gate: dict[str, Any]  # 证据门限判决（pass/weak/reject）
    reasoning: str  # 模型思考流
    conflict: str  # 资料与通用知识的冲突提示