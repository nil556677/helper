"""M5 审批服务 + 总图骨架。"""

from __future__ import annotations  # 延迟值注解

import time  # 时间戳

from app.agent.approval import repository as repo  # 仓储
from app.agent.approval import service as approval_service  # 服务
from app.agent.main_graph import build_main_graph  # 总图
from app.core.db import schema_v1  # 建表
from app.core.db.connection import get_connection  # 连接


def _init(fake_root) -> None:  # 建 schema
    schema_v1.initialize(get_connection())  # 初始化


def _create(**kw) -> str:  # 建一条 pending
    data = dict(
        thread_id="t1", tenant_id="tenant", requester_id="u1", tool_name="query_order",
        args_redacted={"order_id": "1"}, resource_summary="查订单", mutation="read_only",
        risk="medium", approval_reason="人工审批", expires_at=time.time() + 3600,
    )  # 默认
    data.update(kw)  # 覆盖
    return repo.create_request(**data)  # 建


def test_service_pending_and_decide(fake_root) -> None:  # 列表 + 决策
    _init(fake_root)  # 建表
    rid = _create()  # 建
    assert any(item["request_id"] == rid for item in approval_service.pending("tenant"))  # 列表有
    approval_service.decide(rid, "approved", decided_by="admin")  # 批
    assert all(item["request_id"] != rid for item in approval_service.pending("tenant"))  # 不在 pending


def test_service_expire_all(fake_root) -> None:  # 超时扫描
    _init(fake_root)  # 建表
    rid = _create(expires_at=time.time() - 5)  # 已过期
    assert rid in approval_service.expire_all()  # 被标过期


def test_main_graph_builds() -> None:  # 总图可构建
    graph = build_main_graph()  # 构建
    assert hasattr(graph, "ainvoke")  # 可跑


def test_execute_approved_only_when_approved_and_once(fake_root) -> None:  # 批准才执行且幂等
    import asyncio  # 跑异步
    from types import SimpleNamespace  # 假工具

    class _Tool:  # 假工具（记录调用）
        def __init__(self) -> None:
            self.calls = 0  # 次数

        async def ainvoke(self, args):  # 执行
            self.calls += 1  # 记
            return SimpleNamespace(ok=True, data={"ret": args})  # 包络

    _init(fake_root)  # 建表
    tool = _Tool()  # 假工具
    registry = {"query_order": tool}  # 工具表
    rid = _create()  # pending
    blocked = asyncio.run(approval_service.execute_approved(rid, registry=registry))  # pending 不执行
    assert blocked["executed"] is False and tool.calls == 0  # 未批不执行
    approval_service.decide(rid, "rejected", decided_by="admin")  # 拒绝
    rej = asyncio.run(approval_service.execute_approved(rid, registry=registry))  # 拒绝不执行
    assert rej["executed"] is False and tool.calls == 0  # 拒绝不执行
    rid2 = _create(thread_id="t2", args_redacted={"order_id": "9"})  # 另一单
    approval_service.decide(rid2, "approved", decided_by="admin")  # 批
    first = asyncio.run(approval_service.execute_approved(rid2, registry=registry))  # 执行
    assert first["executed"] is True and tool.calls == 1  # 执行一次
    second = asyncio.run(approval_service.execute_approved(rid2, registry=registry))  # 再执行
    assert second["executed"] is False and second["reason"] == "already"  # 幂等
    assert tool.calls == 1  # 只执行一次


def test_decide_and_execute_route_node(fake_root) -> None:  # 路由级决策 + 幂等
    from app.agent.nodes.approval import node as approval_node  # 审批节点

    _init(fake_root)  # 建表
    rid = _create()  # 建一条
    out = approval_node.decide_and_execute(rid, "reject")  # 拒绝
    assert out["ok"] is True and out["executed"] is False  # 拒绝不执行
    again = approval_node.decide_and_execute(rid, "approve")  # 再批（已被拒）
    assert again["ok"] is False and again["reason"] == "already_decided"  # 幂等：不二次生效
    bad = approval_node.decide_and_execute(rid, "other")  # 非法值
    assert bad["ok"] is False  # 忽略