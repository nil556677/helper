"""投影：路由父图节点 → RunEvent（规则/语义不迁移，回答节点→GENERATION_DONE）。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.fsm.projection import project_event  # 投影
from app.agent.fsm.states import RunEvent  # 事件


def test_route_graph_projection() -> None:  # 路由图节点投影
    assert project_event("load_context", {}) is RunEvent.CONTEXT_LOADED  # 上下文
    assert project_event("rule_route", {}) is None  # 规则层不迁移
    assert project_event("semantic_router", {}) is None  # 语义层不迁移
    assert project_event("dispatch_fixed", {}) is RunEvent.GENERATION_DONE  # 固定路径
    assert project_event("llm_fallback", {}) is RunEvent.GENERATION_DONE  # 兜底
    assert project_event("clarify_or_end", {}) is RunEvent.GENERATION_DONE  # 澄清
    assert project_event("finalize", {}) is RunEvent.FINALIZED  # 收尾


def test_legacy_projection_unchanged() -> None:  # 旧图投影不变
    assert project_event("agent", {}) is RunEvent.GENERATION_DONE  # 工具循环
    assert project_event("rag", {"route": {"next": "answer"}}) is RunEvent.RETRIEVAL_DONE  # 检索可答