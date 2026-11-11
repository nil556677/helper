"""M3 正片：自定义 ReAct 图端到端（agent→approval_gate→tools→agent）。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 跑 async

import pytest  # 跳过/断言

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage  # 消息

import app.agent.tools.library as lib  # 内置工具（取 current_time）
from app.agent.react.graph import approval_stats, build_react  # 自定义图 + 计数


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


class _StreamModel:  # 桩模型：只支持 astream（真流式）
    def __init__(self, chunks):  # 分块序列
        self._chunks = list(chunks)  # 存
        self._i = 0  # 步

    def bind_tools(self, tools, **kwargs):  # 绑定（原样返回）
        del tools, kwargs  # 不用
        return self  # 自身

    async def astream(self, messages):  # 流式产出
        del messages  # 不用
        while self._i < len(self._chunks):  # 逐块
            yield self._chunks[self._i]  # 吐
            self._i += 1  # 步进


def test_react_graph_streams_tokens() -> None:  # 真流式：逐 token 透传 sink，最终还原成完整 AIMessage
    model = _StreamModel([AIMessageChunk(content="你"), AIMessageChunk(content="好，")])  # 分块
    got: list[str] = []  # sink 收集
    graph = build_react(model, [lib.current_time], "sys", token_sink=got.append)  # 接流式回调
    result = asyncio.run(graph.ainvoke({  # 跑
        "messages": [HumanMessage(content="hi")], "status": "running", "pending_request_ids": [], "approval_checked": False,
    }))  # 结束
    assert "".join(got) == "你好，"  # 每个文本 token 都被透传给 sink
    last = result["messages"][-1]  # 终答
    assert isinstance(last, AIMessage)  # 必须是完整 AIMessage（不能是 chunk）
    assert str(last.content or "") == "你好，"  # 正文汇聚完整


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
        async def get_order_detail(self, order_id: str):  # 会被记录
            calls.append(order_id)  # 记
            raise AssertionError("审批未拦截，工具被直接执行")  # 不该发生

        async def get_orders_by_phone(self, phone: str):  # 同
            calls.append(phone)  # 记
            raise AssertionError("审批未拦截，工具被直接执行")  # 不该

    order_repo.set_order_repo(_FakeRepo())  # 注入（其方法一调即炸）
    before_blocked = approval_stats()["blocked"]  # 拦截前
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
    assert approval_stats()["blocked"] > before_blocked  # 审批拦截计数增长


def test_end_to_end_approval_flow(fake_root) -> None:  # ReAct→落表→批准→执行
    from app.agent.approval import service as approval_service  # 审批服务
    from app.core.db import schema_v1  # 建表
    from app.core.db.connection import get_connection  # 连接
    from app.services.order import repo as order_repo  # 订单仓储
    from app.services.order.repo import Order  # 订单对象

    schema_v1.initialize(get_connection())  # 建审批表

    class _Repo:  # 假订单库
        def __init__(self) -> None:  # 初始化
            self.calls = 0  # 调用次数

        async def get_order_detail(self, order_id: str):  # 订单+商品
            self.calls += 1  # 记
            return (Order(order_id=order_id, status="已发货", amount="99.5", pay_method="微信", created_at="x", logistics_no="SF1"), [])  # 单+空商品

        async def get_orders_by_phone(self, phone: str):  # 按手机号
            return [Order(order_id="1", status="已发货", amount="99.5", pay_method="微信", created_at="x", logistics_no="SF1")]  # 占位

    fake_repo = _Repo()  # 实例
    order_repo.set_order_repo(fake_repo)  # 注入
    try:  # 跑
        tc = {"name": "query_order", "args": {"order_id": "12345"}, "id": "c2", "type": "tool_call"}  # 订单调用
        model = _Model([AIMessage(content="", tool_calls=[tc]), AIMessage(content="已提交审核")])  # 模型
        graph = build_react(model, [lib.query_order], "sys")  # 建图
        asyncio.run(graph.ainvoke({  # 第一遍：触发审批门
            "messages": [HumanMessage(content="查订单 12345")], "status": "running",
            "pending_request_ids": [], "approval_checked": False, "thread_id": "t", "tenant_id": "default", "requester_id": "u",
        }))  # 结束
    finally:  # 保留注入至执行完成后统一复位
        pass  # 占位（execute 需用同一仓储）
    try:  # 执行 + 复位
        pend = approval_service.pending("default")  # 待审
        assert any(item["tool_name"] == "query_order" for item in pend)  # 落了一条
        rid = pend[0]["request_id"]  # 请求 id
        approval_service.decide(rid, "approved", decided_by="admin")  # 批准
        out = asyncio.run(approval_service.execute_approved(rid))  # 执行
        assert out["executed"] is True  # 批准后才执行
        assert fake_repo.calls == 1  # 工具确实被调用一次（审批通过才放行）
    finally:  # 复位
        order_repo.set_order_repo(None)  # 清