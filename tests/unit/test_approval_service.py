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