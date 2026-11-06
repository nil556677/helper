"""路由父图端到端：direct 路径真实跑到最终（打桩 finalize 避免落库）。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 跑 async 图
from typing import Any  # 任意值

import app.agent.agent as agent_mod  # 图构造


def _run(graph: Any, state: dict[str, Any]) -> dict[str, Any]:  # async 图跑一轮
    return asyncio.run(graph.ainvoke(state))  # ainvoke


def test_route_graph_direct_e2e(monkeypatch) -> None:  # direct 端到端
    async def noop_finalize(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 打桩收尾
        del config  # 不用
        return {}  # 不落库

    monkeypatch.setattr(agent_mod, "finalize_node", noop_finalize)  # 换收尾
    graph = agent_mod.build_route_graph()  # 建图（此时 finalize 已是桩）
    result = _run(  # 跑一轮
        graph,
        {"question": "现在几点", "conversation_id": "c", "turn_id": "", "turns": [], "attachments": []},  # 输入
    )  # 结束
    assert result.get("route", {}).get("intent") == "direct"  # 规则命中
    assert str(result.get("answer") or "").startswith("现在是 ")  # 模板回答


def test_route_graph_ambiguous_goes_to_search_agent(monkeypatch) -> None:  # ambiguous → L3 派搜索 agent，不弹窗
    async def noop_finalize(state, config=None):  # 打桩收尾
        del config  # 不用
        return {}  # 不落库

    async def fake_semantic(state, config=None):  # 打桩语义：强制 ambiguous
        del config  # 不用
        return {"semantic_recall": {"decision": "ambiguous", "candidates": [], "top1": 0.5, "margin": 0.05}}  # 召回

    monkeypatch.setattr(agent_mod, "finalize_node", noop_finalize)  # 换收尾
    monkeypatch.setattr(agent_mod, "semantic_node", fake_semantic)  # 换语义
    graph = agent_mod.build_route_graph()  # 建图
    result = _run(  # 跑一轮
        graph,
        {"question": "帮我安排一个复杂任务", "conversation_id": "c", "turn_id": "", "turns": [], "attachments": []},  # 输入
    )  # 结束
    assert result.get("fallback_used")  # 走了 l3 搜索 agent（llm_fallback），不给秋清弹窗
    assert not result.get("clarify_options")  # 不再让用户选