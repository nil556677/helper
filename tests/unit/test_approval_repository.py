"""M1 审批落表：幂等插入 / 乐观决策 / 审计 / 超时。"""

from __future__ import annotations  # 延迟值注解

import time  # 时间戳

import pytest  # 断言异常

from app.agent.approval import repository as repo  # 仓储
from app.core.db import schema_v1  # 建表
from app.core.db.connection import get_connection  # 连接


def _init(fake_root) -> None:  # 建 schema
    schema_v1.initialize(get_connection())  # 初始化


def _base(**kw) -> dict:  # 基础参数
    data = {  # 默认
        "thread_id": "t1", "tenant_id": "tenant", "requester_id": "u1",
        "tool_name": "query_order", "args_redacted": {"order_id": "12345"},
        "resource_summary": "查询订单 12345 的详情", "mutation": "read_only",
        "risk": "medium", "approval_reason": "人工审批", "expires_at": time.time() + 3600,
    }  # 默认
    data.update(kw)  # 覆盖
    return data  # 返回


def test_create_idempotent(fake_root) -> None:  # 同参同线程幂等
    _init(fake_root)  # 建表
    a = repo.create_request(**_base())  # 第一次
    b = repo.create_request(**_base())  # 第二次（同参）
    assert a == b  # 同一条
    assert repo.list_pending("tenant")  # 待审有一条


def test_decide_optimistic_conflict(fake_root) -> None:  # 乐观并发：二次决策冲突
    _init(fake_root)  # 建表
    rid = repo.create_request(**_base())  # 建
    repo.decide(rid, "approved", "admin")  # 第一次通过
    with pytest.raises(repo.ApprovalConflict):  # 期望冲突
        repo.decide(rid, "rejected", "admin")  # 二次拒绝 → 已被处理


def test_expire_pending(fake_root) -> None:  # 超时扫描
    _init(fake_root)  # 建表
    rid = repo.create_request(**_base(expires_at=time.time() - 10))  # 已过期
    expired = repo.expire_pending(now=time.time())  # 扫描
    assert rid in expired  # 被标过期
    assert not repo.list_pending("tenant")  # 不再待审