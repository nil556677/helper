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


def test_react_graph_approval_intercepts_manual_tool(monkeypatch) -> None:  # manual 工具被审批门拦截、不执行
    from langchain_core.messages import ToolMessage  # 找拦截消息

    from app.services.order import repo as order_repo  # 订单仓储

    calls: list[str] = []  # 记录工具是否真执行

    class _FakeRepo:  # 假订单库（若被调用说明没拦住）
        async def get_order(self, order_id: str):  # 会被记录
            calls.append(order_id)  # 记
            raise AssertionError("审批未拦截，工具被直接执行")  # 不该发生

        async def get_orders_by_phone(self, phone: str):  # 同
            calls.append(phone)  # 记
            raise AssertionError("审批未拦截，工具被直接执行")  # 不该

    order_repo.set_order_repo(_FakeRepo())  # 注入（其方法一调即炸）
    try:  # 跑
        tc = {"name": "query_order", "args": {"order_id": "12345"}, "id": "c1", "type": "tool_call"}  # 订单调用
        model = _Model([  # 两次响应
            AIMessage(content="", tool_calls=[tc]),  # 要调订单
            AIMessage(content="该查询需要人工审批，已提交审核。"),  # 见拦截后终答
        ])  # 结束
        graph = build_react(model, [lib.query_order], "sys")  # 建图（query_order approval=manual）
        result = asyncio.run(graph.ainvoke({  # 跑
            "messages": [HumanMessage(content="查订单 12345")], "status": "running",
            "pending_request_ids": [], "approval_checked": False, "thread_id": "t1",
        }))  # 结束
    finally:  # 复位
        order_repo.set_order_repo(None)  # 清
    msgs = result["messages"]  # 全部消息
    assert any(isinstance(m, ToolMessage) and "需要人工审批" in str(m.content) for m in msgs)  # 被拦截
    assert calls == []  # 工具未被真正执行
    assert str(result["messages"][-1].content or "")  # 回了模型并给终答