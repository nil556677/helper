"""M8：general_agent 兜底 + 澄清闭环（pending_clarify 短路）。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 跑 async
from types import SimpleNamespace  # 假响应

from app.agent.llm.provider import set_chat_model_provider  # 模型提供者
from app.agent.nodes.general.node import general_agent_node  # 兜底节点
from app.agent.nodes.triage.node import triage_node  # 分诊节点


def test_general_agent_degrades_without_model() -> None:  # 无模型 → 模板
    set_chat_model_provider(None)  # 清提供者
    result = asyncio.run(general_agent_node({"question": "帮我写诗"}))  # 跑
    assert "资料问答助手" in result["answer"]  # 模板


def test_general_agent_with_model() -> None:  # 有模型 → 模型话术
    class _Model:  # 假模型
        async def ainvoke(self, messages):  # 调
            del messages  # 不用
            return SimpleNamespace(content="我可以帮你检索资料。")  # 回答

    set_chat_model_provider(lambda: _Model())  # 注入
    try:  # 清理
        result = asyncio.run(general_agent_node({"question": "帮我写诗"}))  # 跑
        assert result["answer"] == "我可以帮你检索资料。"  # 模型话术
    finally:  # 复位
        set_chat_model_provider(None)  # 清


def test_triage_clarify_short_circuit() -> None:  # pending_clarify 短路
    out = triage_node(  # 跑
        {
            "question": "查时间",
            "preprocessed": {"query": "查时间", "phase": "followup"},
            "pending_clarify": {"choices": {"查时间": "time"}},
        }
    )
    assert out["triage"]["path_type"] == "fast_single"  # 直接
    assert out["triage"]["escalate_reason"] == "clarify_resolved"  # 标记
    assert out["route"]["route"] == "time"  # 落到固定路径