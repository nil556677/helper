"""兜底单一 ReAct：输入组装、工具事件、答案提取、防循环追问。多样性正反/简单复杂。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 跑 async
from types import SimpleNamespace  # 假图

import pytest  # 框架
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # 消息

import app.agent.fallback.agent as agent_mod  # 被测
from app.agent.context import RunContext  # 上下文


def _fake_graph(result_messages: list, *, tool_events: bool = True) -> SimpleNamespace:  # 假图
    """构建返回固定消息列表的假 graph。"""
    class _G:  # 假图
        async def ainvoke(self, inp):  # 返回固定结果
            del inp  # 忽略输入
            return {"messages": result_messages}  # 固定

    return _G()  # 返回


def _patch_graph(monkeypatch, messages):  # 打桩 _build_graph
    monkeypatch.setattr(agent_mod, "_build_graph", lambda model, prompt: _fake_graph(messages))  # 替换
    return agent_mod._calls if hasattr(agent_mod, "_calls") else None  # 兼容


@pytest.fixture(autouse=True)  # 每个用例独立
def _no_model_cache(monkeypatch):
    yield  # 不缓存


def test_answer_is_plain_final() -> None:  # 简单：直接给答案
    # 准备：一条无工具调的 AIMessage 收尾
    msg = AIMessage(content="年假按累计工作年限折算。")  # 终答
    monkeypatch = pytest.MonkeyPatch()  # 打桩
    monkeypatch.setattr(agent_mod, "_build_graph", lambda model, prompt: _fake_graph([msg]))
    try:
        out = asyncio.run(agent_mod.run_react({"question": "年假怎么算"}, model=object()))
    finally:
        monkeypatch.undo()
    assert "年假" in out["answer"]  # 正文
    assert out["fallback_used"] is True  # 标记


def test_answer_after_tool_calls() -> None:  # 复杂：先调工具再答
    call = {"name": "query_weather", "args": {"city": "成都"}, "id": "1"}  # 调用
    msgs = [  # 消息序列
        AIMessage(content="", tool_calls=[call]),  # 调工具
        ToolMessage(content="成都：多云 17℃", name="query_weather", tool_call_id="1", status="success"),  # 结果
        AIMessage(content="成都天气：多云 17℃。"),  # 终答
    ]  # 结束
    ctx = RunContext(run_id="r", question="成都天气", conversation_id="c", emit=lambda e: None)  # 上下文
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_mod, "_build_graph", lambda model, prompt: _fake_graph(msgs))
    try:
        from app.agent.context import run_context  # 注入上下文

        with run_context(ctx):  # 绑 run（验证 TOOL 事件不崩）
            out = asyncio.run(agent_mod.run_react({"question": "成都天气"}, model=object()))
    finally:
        monkeypatch.undo()
    assert "成都" in out["answer"]  # 用到工具结果


def test_too_long_answer_still_ok() -> None:  # 简单：长回答以?结尾不误判为提问
    long = "这是一段非常长的解释，覆盖了员工入职、转正、请假、年假折算和离职交接等多个方面的完整规定与细节流程说明，请问你是否已经理解清楚了？"  # 长句
    assert len(long) > 60  # 确认超长
    msg = AIMessage(content=long)  # 终答
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_mod, "_build_graph", lambda m, p: _fake_graph([msg]))
    try:
        out = asyncio.run(agent_mod.run_react({"question": "q", "clarify_count": 0}, model=object()))
    finally:
        monkeypatch.undo()
    assert out["clarify_count"] == 0  # 长句不算追问


def test_question_answer_increments_clarify() -> None:  # 防循环：短提问 → clarify_count+1
    msg = AIMessage(content="您是想问天气还是资料？")  # 短句提问
    ctx = RunContext(run_id="r", question="q", conversation_id="c", emit=lambda e: None)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_mod, "_build_graph", lambda m, p: _fake_graph([msg]))
    try:
        from app.agent.context import run_context

        with run_context(ctx):
            out = asyncio.run(agent_mod.run_react({"question": "q", "clarify_count": 0}, model=object()))
    finally:
        monkeypatch.undo()
    assert out["clarify_count"] == 1  # 记一次追问


def test_intent_confirmed_no_increment() -> None:  # 防循环：已确认意图，提问不算追问
    msg = AIMessage(content="请确认下您要查哪方面？")  # 提问
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_mod, "_build_graph", lambda m, p: _fake_graph([msg]))
    try:
        out = asyncio.run(agent_mod.run_react({"question": "q", "intent_confirmed": True}, model=object()))
    finally:
        monkeypatch.undo()
    assert out["clarify_count"] == 0  # 已确认不递增


def test_confirmed_reask_overridden() -> None:  # 防循环：已确认还追问 → 强制诚实收尾，不再循环
    msg = AIMessage(content="那你到底要查哪个？")  # 第二次还问
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_mod, "_build_graph", lambda m, p: _fake_graph([msg]))
    try:
        out = asyncio.run(agent_mod.run_react({"question": "q", "clarify_count": 1}, model=object()))  # 上一轮已追问 → confirmed
    finally:
        monkeypatch.undo()
    assert not out["answer"].endswith(("？", "?"))  # 不再以提问收尾
    assert "换一种" in out["answer"]  # 诚实收尾话术
    assert out["clarify_count"] == 1  # 不无限累加


def test_user_message_contains_candidate() -> None:  # 候选带过去
    captured = {}  # 捕获输入

    class _G:  # 假图
        async def ainvoke(self, inp):  # 捕获 input
            captured["human"] = inp["messages"][0].content  # 保存 human
            return {"messages": [AIMessage(content="答")]}  # 固定终答

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_mod, "_build_graph", lambda m, p: _G())  # 替换
    try:
        asyncio.run(agent_mod.run_react(  # 跑
            {"question": "多候选", "semantic_recall": {"candidates": [{"route": "weather", "score": 0.5}, {"route": "knowledge", "score": 0.4}]}},
            model=object(),
        ))  # 结束
    finally:
        monkeypatch.undo()
    assert "weather" in captured["human"]  # 候选里有 weather
    assert "knowledge" in captured["human"]  # 候选里有 knowledge


def test_user_message_offers_guidance_when_no_candidate() -> None:  # miss：候选空 → 提示语
    captured = {}  # 捕获

    class _G:
        async def ainvoke(self, inp):
            captured["human"] = inp["messages"][0].content
            return {"messages": [AIMessage(content="答")]}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_mod, "_build_graph", lambda m, p: _G())
    try:
        asyncio.run(agent_mod.run_react({"question": "无匹配"}, model=object()))
    finally:
        monkeypatch.undo()
    assert "未匹配到明确功能" in captured["human"]  # miss 标记


def test_hard_no_reask_when_over_budget() -> None:  # 防循环：已问过 → 注入严禁再问
    captured = {}  # 捕获

    class _G:
        async def ainvoke(self, inp):
            captured["human"] = inp["messages"][0].content
            return {"messages": [AIMessage(content="答")]}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_mod, "_build_graph", lambda m, p: _G())
    try:
        asyncio.run(agent_mod.run_react({"question": "q", "clarify_count": 1}, model=object()))
    finally:
        monkeypatch.undo()
    assert "严禁" in captured["human"]  # 强约束注入
    assert "追问" in captured["human"]


def test_memory_included() -> None:  # 多轮记忆带过去
    captured = {}  # 捕获

    class _G:
        async def ainvoke(self, inp):
            captured["human"] = inp["messages"][0].content
            return {"messages": [AIMessage(content="答")]}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_mod, "_build_graph", lambda m, p: _G())
    try:
        asyncio.run(agent_mod.run_react(  # 跑
            {"question": "q", "turns": [{"user": "上轮问题", "assistant": "上轮答案"}]}, model=object())
        )  # 结束
    finally:
        monkeypatch.undo()
    assert "上轮问题" in captured["human"]  # 记忆里有上轮
    assert "上轮答案" in captured["human"]


def test_empty_answer_falls_back() -> None:  # 空终答 → 兜底话术
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_mod, "_build_graph", lambda m, p: _fake_graph([AIMessage(content="")]))
    try:
        out = asyncio.run(agent_mod.run_react({"question": "q"}, model=object()))
    finally:
        monkeypatch.undo()
    assert out["answer"]  # 非空兜底话术


def test_guard_derive_from_memory() -> None:  # 跨轮护栏：记忆反推追问数/确认态
    from langchain_core.messages import AIMessage as _A  # 复用消息（构造 Turn 用）

    class _T:  # 假 Turn
        def __init__(self, question, answer):  # 字段（映射到 Turn.user/assistant）
            self.user = question  # 用户问题
            self.assistant = answer  # 助手回答

    # 场景1：agent 问过一次，用户补充 → 已确认、追问=1
    turns = [_T("什么", "您想查天气还是资料？"), _T("查天气", "好，成都天气多云。")]  # 追问后用户答复
    count, confirmed = agent_mod.guard_from_memory(turns)
    assert count == 1  # 一次追问
    assert confirmed is True  # 已确认

    # 场景2：只有一次追问，还没答复 → 未确认，追问=1
    turns2 = [_T("什么", "您想查哪方面？")]  # 仅追问
    c2, f2 = agent_mod.guard_from_memory(turns2)
    assert c2 == 1 and f2 is False  # 未确认

    # 场景3：正常运行（无追问）→ 0/False
    c3, f3 = agent_mod.guard_from_memory([_T("年假怎么算", "年假按累计工龄折算。")])
    assert c3 == 0 and f3 is False  # 未追问未确认


class _FakeTool:  # 假检索工具
    def __init__(self, data):  # 构造
        self._data = data  # 固定出参

    async def ainvoke(self, args):  # 返回结构化包络
        del args  # 忽略
        return SimpleNamespace(data=self._data)  # 包络


def _run_with(messages, monkeypatch):  # 用固定消息跑 run_react
    monkeypatch.setattr(agent_mod, "_build_graph", lambda m, p: _fake_graph(messages))  # 替换图
    try:  # 跑
        return asyncio.run(agent_mod.run_react({"question": "q"}, model=object()))  # 结果
    finally:  # 复位
        monkeypatch.undo()  # 恢复


def test_weather_answer_has_no_citations(monkeypatch) -> None:  # 天气回答不需要引用
    call = {"name": "query_weather", "args": {"city": "成都"}, "id": "1"}  # 调天气
    messages = [  # 消息序列
        AIMessage(content="", tool_calls=[call]),  # 调工具
        ToolMessage(content="成都 多云 17℃", name="query_weather", tool_call_id="1", status="success"),  # 结果
        AIMessage(content="成都今天多云，17℃。"),  # 终答
    ]  # 结束
    out = _run_with(messages, monkeypatch)  # 跑
    assert out["fragments"] == []  # 不产生引用
    assert "参考" not in out["answer"]  # 不加角标


def test_rag_answer_has_citations(monkeypatch) -> None:  # 知识库检索才产生引用
    fake = _FakeTool({"evidence": [{"text": "年假 5 天", "source_name": "制度", "origin": "第 1 段"}], "gate": {"decision": "pass"}})  # 假工具
    monkeypatch.setattr("app.agent.tools.catalog.tool_catalog", lambda: {"search_knowledge_base": fake})  # 打桩目录
    call = {"name": "search_knowledge_base", "args": {"query": "年假"}, "id": "1"}  # 调检索
    messages = [  # 消息序列
        AIMessage(content="", tool_calls=[call]),  # 调工具
        ToolMessage(content="[1] 制度：年假 5 天", name="search_knowledge_base", tool_call_id="1", status="success"),  # 结果
        AIMessage(content="年假 5 天。"),  # 终答
    ]  # 结束
    out = _run_with(messages, monkeypatch)  # 跑
    assert len(out["fragments"]) == 1  # 有引用
    assert "参考：[1]" in out["answer"]  # 补了角标


def test_rag_weak_gate_sets_note(monkeypatch) -> None:  # 证据弱 → 门禁提示
    fake = _FakeTool({"evidence": [{"text": "相关片段", "source_name": "资料"}], "gate": {"decision": "weak"}})  # 假工具
    monkeypatch.setattr("app.agent.tools.catalog.tool_catalog", lambda: {"search_knowledge_base": fake})  # 打桩
    call = {"name": "search_knowledge_base", "args": {"query": "x"}, "id": "1"}  # 调检索
    messages = [  # 消息
        AIMessage(content="", tool_calls=[call]),  # 调
        ToolMessage(content="[1] 资料", name="search_knowledge_base", tool_call_id="1", status="success"),  # 结果
        AIMessage(content="根据资料……"),  # 终答
    ]  # 结束
    out = _run_with(messages, monkeypatch)  # 跑
    assert "证据较弱" in out["note"]  # 门禁提示生效