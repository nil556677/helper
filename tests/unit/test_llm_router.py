"""L2 LLM 兜底：无模型降级、有模型分类、节点写 intent。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.contracts.triage import IntentDecision  # 契约
from app.agent.nodes.route_llm import node as node_mod  # 节点模块
from app.agent.routing.llm_router import classify, degrade  # 分类


class _Runner:  # 假结构化 runner
    async def ainvoke(self, messages):  # 调
        del messages  # 不用
        return IntentDecision(intent="query", confidence=0.8, source="llm")  # 固定


class _Model:  # 假模型
    def with_structured_output(self, schema):  # 结构化
        del schema  # 不用
        return _Runner()  # 返回 runner


async def test_classify_degrades_without_model() -> None:  # 无模型
    decision = await classify("q", [], None)  # 分类
    assert decision.intent == "clarify"  # 降级
    assert degrade("x").intent == "clarify"  # 直接降级


async def test_classify_with_model() -> None:  # 有模型
    decision = await classify("q", [{"route": "knowledge"}], _Model())  # 分类
    assert decision.intent == "query" and decision.confidence == 0.8  # 结果


async def test_classify_swallows_model_error() -> None:  # 模型异常降级
    class _Bad:  # 坏模型
        def with_structured_output(self, schema):  # 结构化
            del schema  # 不用
            raise RuntimeError("boom")  # 失败

    decision = await classify("q", [], _Bad())  # 分类
    assert decision.intent == "clarify"  # 降级


async def test_route_llm_node_writes_intent() -> None:  # 节点写 intent
    node_mod.set_model_provider(lambda: _Model())  # 注入模型
    try:  # 清理
        out = await node_mod.route_llm_node({"question": "q", "semantic_recall": {"candidates": [{"route": "knowledge"}]}})  # 跑
        assert out["intent"]["intent"] == "query"  # 写入
    finally:  # 复位
        node_mod.set_model_provider(None)  # 清
    out2 = await node_mod.route_llm_node({"question": "q"})  # 无模型
    assert out2["intent"]["intent"] == "clarify"  # 降级