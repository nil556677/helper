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
    triage: dict[str, Any]  # TriageDecision 的 JSON 形态（L3 产物）
    escalated_this_session: bool  # 本会话是否已升级过（防乒乓）
    last_turn_status: str  # 上一轮运行状态（done/failed/…，供重试判定）
    last_failed_question: str  # 上一轮失败的问题（前置分诊判"相似→重试"用）
    dependency_agentic: bool  # 依赖链 LLM 兜底结论（规则词表未命中时上游判定）
    escalate_reason: str  # 快捷搜索质量不足的升级原因（空=不升级）
    verify: dict[str, Any]  # VerifyResult 的 JSON 形态（L5 校验）
    hitl_enabled: bool  # 是否启用 HITL（生产图带 checkpointer 时开）
    run_id: str  # 运行 id（HITL 负载用）
    thread_id: str  # 恢复用线程 id
    flags: dict[str, Any]  # 硬约束标志（L3 短路读）
    pending_clarify: dict[str, Any]  # 上轮待澄清（下一轮 phase=followup）
    pending_param: str  # 待填的必需参数值（追问后用户答复）
    pending_param_name: str  # 该值对应的必需参数名（city/date…）
    reroute: bool  # 澄清自由输入后标记重路由（after_clarify 读它）
    rule_hits: list[dict[str, Any]]  # 规则层命中明细（可观测）
    semantic_recall: dict[str, Any]  # SemanticRecall 的 JSON 形态（语义层只写）

    # 防循环追问（兜底 ReAct 护栏）
    intent_confirmed: bool  # 意图是否已确认（真 → 不再"信息不足→追问"）
    clarify_count: int  # 本会话 agent 追问次数（超上限即熔断）

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