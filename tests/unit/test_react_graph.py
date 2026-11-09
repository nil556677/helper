"""M3 正片：自定义 ReAct 图端到端（agent→approval_gate→tools→agent）。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 跑 async

from langchain_core.messages import AIMessage, HumanMessage  # 消息

import app.agent.tools.library as lib  # 内置工具（取 current_time）
from app.agent.react.graph import build_react  # 自定义图


class _Model:  # 桩模型：两次调用（先调工具，再给终答）
    def __init__(self, responses):  # 响应序列
        self._responses = list(responses)  # 存
        self._i = 0  # 步

    def bind_tools(self, tools, **kwargs):  # 绑定（原样返回）
        del tools, kwargs  # 不用
        return self  # 自身

    async def ainvoke(self, messages):  # 调
        del messages  # 不用
        resp = self._responses[self._i]  # 取
        self._i += 1  # 步进
        return resp  # 返回


def test_react_graph_single_tool_roundtrip() -> None:  # 工具调用 → 无审批直通 → 回模型
    tool_call = {"name": "current_time", "args": {"as_timestamp": False}, "id": "1", "type": "tool_call"}  # 工具调用
    model = _Model([  # 两次响应
        AIMessage(content="", tool_calls=[tool_call]),  # 第 1 次：要调工具
        AIMessage(content="现在是 17:00:00"),  # 第 2 次：终答
    ])  # 结束
    graph = build_react(model, [lib.current_time], "你是助手。")  # 建图（current_time approval=none 直通）
    result = asyncio.run(graph.ainvoke({  # 跑
        "messages": [HumanMessage(content="现在几点")],  # 输入
        "status": "running",  # 状态
        "pending_request_ids": [],  # 无待审
        "approval_checked": False,  # 未审
    }))  # 结束
    last = result["messages"][-1]  # 终答
    assert isinstance(last, AIMessage) and not getattr(last, "tool_calls", None)  # 无待执行调用
    assert str(last.content or "")  # 有终答


def test_react_graph_direct_answer() -> None:  # 不调工具直接答
    model = _Model([AIMessage(content="你好")])  # 一次终答
    graph = build_react(model, [lib.current_time], "sys")  # 建图
    result = asyncio.run(graph.ainvoke({  # 跑
        "messages": [HumanMessage(content="hi")], "status": "running", "pending_request_ids": [], "approval_checked": False,
    }))  # 结束
    assert str(result["messages"][-1].content or "") == "你好"  # 终答