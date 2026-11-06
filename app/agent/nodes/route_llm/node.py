"""L2 LLM 兜底节点：ambiguous 时补一次轻量意图分类，写 `intent`。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, Callable  # 类型

from app.agent.contracts.triage import IntentDecision  # 契约
from app.agent.routing.llm_router import classify  # 分类

__all__ = ["route_llm_node", "set_model_provider"]  # 导出

#: 模型提供者（装配层注入 gateway 产出的 chat_model；默认无模型→降级）。
_MODEL_PROVIDER: Callable[[], Any] | None = None  # 提供者


def set_model_provider(provider: Callable[[], Any] | None) -> None:  # 注入模型来源
    global _MODEL_PROVIDER  # 改模块级
    _MODEL_PROVIDER = provider  # 存


def _model() -> Any | None:  # 取模型
    return _MODEL_PROVIDER() if _MODEL_PROVIDER is not None else None  # 无则 None


async def route_llm_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """读语义候选 → 轻量分类 → 写 `intent`；无条件边在此重算。"""
    del config  # 不用图配置
    recall = state.get("semantic_recall") or {}  # 语义召回
    candidates = recall.get("candidates") if isinstance(recall, dict) else []  # 候选
    decision: IntentDecision = await classify(str(state.get("question") or ""), candidates or [], _model())  # 分类
    return {"intent": decision.model_dump(mode="json")}  # 写回