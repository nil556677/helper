"""L2 LLM 兜底节点：ambiguous 时补一次轻量意图分类，写 `intent`。"""

from langchain_core.runnables import RunnableConfig  # 节点 config 类型（LangGraph）

from typing import Any, Callable  # 类型

from app.agent.contracts.triage import IntentDecision  # 契约
from app.agent.llm.provider import get_chat_model  # 统一模型入口（唯一，避免 provider 分叉）
from app.agent.routing.llm_router import classify  # 分类

__all__ = ["route_llm_node", "set_model_provider"]  # 导出

#: 兼容旧注入接缝（新代码用 llm.provider；保留供测试）。
_MODEL_PROVIDER: Callable[[], Any] | None = None  # 提供者


def set_model_provider(provider: Callable[[], Any] | None) -> None:  # 注入模型来源（兼容）
    global _MODEL_PROVIDER  # 改模块级
    _MODEL_PROVIDER = provider  # 存


def _model() -> Any | None:  # 取模型（统一走 llm.provider，测试注入仍可覆盖）
    if _MODEL_PROVIDER is not None:  # 显式注入优先（测试）
        return _MODEL_PROVIDER()  # 返回
    return get_chat_model()  # 生产走统一入口


async def route_llm_node(state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:  # 图节点
    """读语义候选 → 轻量分类 → 写 `intent`；依赖链 LLM 兜底仅窄缝触发。"""
    del config  # 不用图配置
    question = str(state.get("question") or "")  # 问题
    recall = state.get("semantic_recall") or {}  # 语义召回
    candidates = recall.get("candidates") if isinstance(recall, dict) else []  # 候选
    model = _model()  # 模型（可能 None）
    decision: IntentDecision = await classify(question, candidates or [], model)  # 意图分类
    # —— 依赖链 LLM 兜底：仅"词表未命中 + 已判 query"时补一次二分类 ——
    dependency = False  # 默认不走多步
    if decision.intent == "query":  # 只在查询意图下判依赖
        from app.agent.routing.dependency import needs_agentic  # 依赖判定
        from app.agent.routing.triage import AGENTIC_MARKERS, MULTI_MARKERS  # 词表

        if not any(word in question for word in (*AGENTIC_MARKERS, *MULTI_MARKERS)):  # 词表未命中
            dependency = await needs_agentic(question, model)  # LLM 兜底一次
    return {"intent": decision.model_dump(mode="json"), "dependency_agentic": dependency}  # 写回