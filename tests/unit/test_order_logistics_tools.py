"""M2 订单/物流工具：注入假仓储与假第三方 provider 验证。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 跑异步
import importlib  # 取工具模块

import pytest  # 断言异常

from app.agent.tools.catalog import tool_catalog  # 目录
from app.agent.tools.wiring import describe, unwrap_tool_output  # 描述/还原
from app.services.logistics import client as logistics_client  # 物流 provider
from app.services.order import repo as order_repo  # 订单仓储
from app.services.order.repo import Order  # 订单对象

order_mod = importlib.import_module("app.agent.tools.library.order.query_order")  # 订单工具
log_mod = importlib.import_module("app.agent.tools.library.logistics.query_logistics")  # 物流工具


class _FakeRepo:  # 假订单仓储
    async def get_order(self, order_id: str):  # 按单号
        if order_id == "12345":  # 存在
            return Order(order_id="12345", status="已发货", amount="99.5", pay_method="微信", created_at="2026-11-08", logistics_no="SF123", tenant_id="t")  # 单
        return None  # 不存在

    async def get_orders_by_phone(self, phone: str):  # 按手机号
        return [Order(order_id="12345", status="已发货", amount="99.5", pay_method="微信", created_at="2026-11-08", logistics_no="SF123", tenant_id="t")]  # 一单


class _FakeProvider:  # 假物流 provider
    async def trace(self, tracking_no: str):  # 查轨迹
        return {"carrier": "顺丰", "status": "运输中", "detail": "在杭州中转", "events": [{"at": "now", "desc": "中转"}]}  # 轨迹


@pytest.fixture(autouse=True)  # 注入假外部
def _fake(monkeypatch):
    from app.services.order import repo as or_  # 仓储模块

    from app.services.logistics import client as lc  # provider 模块

    or_.set_order_repo(_FakeRepo())  # 注入订单
    lc.set_provider(_FakeProvider())  # 注入物流
    yield  # 跑
    or_.set_order_repo(None)  # 复位
    lc.set_provider(None)  # 复位


def test_query_order_by_id() -> None:  # 按订单号查订单
    env = asyncio.run(order_mod.query_order.ainvoke({"order_id": "12345"}))  # 调
    out = unwrap_tool_output(env)  # 还原
    assert out.ok and out.data["order_id"] == "12345"  # 单号
    assert "已发货" in out.data["status"]  # 状态


def test_query_order_missing_args() -> None:  # 双缺 → 缺参错
    env = asyncio.run(order_mod.query_order.ainvoke({"order_id": "", "phone": ""}))  # 调
    out = unwrap_tool_output(env)  # 还原
    assert out.ok is False  # 失败
    assert "订单号" in out.message  # 提示


def test_query_logistics_by_order_resolves_tracking() -> None:  # order_id → 运单号 → 第三方
    env = asyncio.run(log_mod.query_logistics.ainvoke({"order_id": "12345"}))  # 调
    out = unwrap_tool_output(env)  # 还原
    assert out.ok and out.data["tracking_no"] == "SF123"  # 解析到运单号
    assert out.data["carrier"] == "顺丰"  # 第三方返回


def test_query_logistics_by_tracking_direct() -> None:  # 直接运单号
    env = asyncio.run(log_mod.query_logistics.ainvoke({"tracking_no": "SF123"}))  # 调
    out = unwrap_tool_output(env)  # 还原
    assert out.ok and out.data["carrier"] == "顺丰"  # 走了第三方


def test_tools_in_catalog_and_approval() -> None:  # 目录 + 审批契约
    catalog = tool_catalog()  # 目录
    assert "query_order" in catalog and "query_logistics" in catalog  # 已注册
    assert describe(catalog["query_order"]).approval == "manual"  # 订单需人工审批
    assert describe(catalog["query_logistics"]).approval == "none"  # 物流不审批
    assert describe(catalog["query_order"]).risk == "medium"  # 中风险


def test_tool_selection_descriptions_distinct() -> None:  # 工具选择：描述互指向、可区分
    catalog = tool_catalog()  # 目录
    order = str(catalog["query_order"].description or "")  # 订单描述
    logis = str(catalog["query_logistics"].description or "")  # 物流描述
    search = str(catalog["search_knowledge_base"].description or "")  # 检索描述
    assert "query_logistics" in order and "search_knowledge_base" in order  # 订单指认其它
    assert "query_order" in logis and "search_knowledge_base" in logis  # 物流指认其它
    assert "订单" not in search  # 检索不查订单
    assert "物流" not in search or "不查" in search  # 检索不提物流轨迹（或明确排除）