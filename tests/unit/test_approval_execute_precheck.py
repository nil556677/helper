"""审批「批准后执行」增强：edited_args 优先 + 执行前 schema 复核拦截非法参数。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 跑异步
import time  # 时间戳
from types import SimpleNamespace  # 假工具

from pydantic import BaseModel, Field  # 参数契约（用于复核）

from app.agent.approval import repository as repo  # 仓储
from app.agent.approval import service as approval_service  # 服务
from app.core.db import schema_v1  # 建表
from app.core.db.connection import get_connection  # 连接


def _init(fake_root) -> None:  # 建 schema
    schema_v1.initialize(get_connection())  # 初始化


def _create(tool_name="query_order", args=None, **kw) -> str:  # 建一条 pending
    data = dict(  # 默认
        thread_id="t1", tenant_id="tenant", requester_id="u1", tool_name=tool_name,  # 基础
        args_redacted=dict(args or {"order_id": "1"}),  # 参数快照
        resource_summary="查订单", mutation="read_only", risk="medium",  # 摘要/画像/风险
        approval_reason="人工审批", expires_at=time.time() + 3600,  # 原因/过期
    )  # 默认结束
    data.update(kw)  # 覆盖
    return repo.create_request(**data)  # 建


class _RecordingTool:  # 记录收到的参数
    """真工具：记录 ainvoke 收到的 args，返回成功包络。"""

    def __init__(self) -> None:  # 构造
        self.last_args: dict | None = None  # 收到的参数
        self.calls = 0  # 次数

    async def ainvoke(self, args):  # 执行
        self.calls += 1  # 记次
        self.last_args = dict(args)  # 记录
        return SimpleNamespace(ok=True, data={"ret": args})  # 包络


def test_execute_prefers_edited_args(fake_root) -> None:  # 人工编辑参数优先
    _init(fake_root)  # 建表
    tool = _RecordingTool()  # 真工具
    rid = _create(args={"order_id": "1"})  # 挂单（原参）
    repo.decide(rid, "approved", decided_by="admin", edited_args={"order_id": "9"})  # 人工改成 9
    out = asyncio.run(approval_service.execute_approved(rid, registry={"query_order": tool}))  # 执行
    assert out["executed"] is True  # 已执行
    assert tool.last_args == {"order_id": "9"}  # 用编辑后的参数执行


class _StrictSchema(BaseModel):  # 带约束的参数契约
    """order_id 至少 5 位才能过校验（用于拦截非法执行参数）。"""

    order_id: str = Field(min_length=5)  # 最短 5


def _desc_with_schema(schema) -> SimpleNamespace:  # 带 args_schema 的假工具
    return SimpleNamespace(  # 工具
        name="query_order",  # 名
        description="查订单",  # 说明
        args_schema=schema,  # 参数契约
        metadata={"dsh": {"version": 1, "mutation": "read_only", "approval": "manual", "risk": "medium", "permissions": (), "timeout_ms": 8000}},  # 元数据
    )  # 结束


def test_execute_blocks_invalid_args_by_schema(fake_root) -> None:  # 非法参数被拦截
    _init(fake_root)  # 建表
    tool = _desc_with_schema(_StrictSchema)  # 带契约工具（无 ainvoke）
    rid = _create(args={"order_id": "ab"})  # 过短参数（<5 位）
    repo.decide(rid, "approved", decided_by="admin")  # 批
    out = asyncio.run(approval_service.execute_approved(rid, registry={"query_order": tool}))  # 执行
    assert out["executed"] is False  # 被拦截不执行
    assert "order_id" in str(out.get("reason") or "")  # 原因指向该字段


def test_execute_without_schema_passes(fake_root) -> None:  # 拿不到 schema 照常执行（向后兼容）
    _init(fake_root)  # 建表
    tool = _RecordingTool()  # 无 schema 的普通工具
    rid = _create(args={"order_id": "1"})  # 挂单
    repo.decide(rid, "approved", decided_by="admin")  # 批
    out = asyncio.run(approval_service.execute_approved(rid, registry={"query_order": tool}))  # 执行
    assert out["executed"] is True  # 照常执行
    assert tool.last_args == {"order_id": "1"}  # 参数正确