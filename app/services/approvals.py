"""审批对外服务（UI/API 走这里；避免 UI 直接碰 app.core）。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 跑异步
from typing import Any  # 任意值

from app.agent.approval import service as approval_service  # 审批服务

__all__ = ["decide", "execute", "expire", "pending"]  # 导出


def _tenant() -> str:  # 默认租户
    return "default"  # 单租户占位


def pending(*, limit: int = 100) -> list[dict[str, Any]]:  # 待审列表
    return approval_service.pending(_tenant(), limit=limit)  # 委托


def decide(request_id: str, action: str, *, decided_by: str = "admin", note: str = "") -> None:  # 决策
    return approval_service.decide(request_id, action, decided_by=decided_by, note=note)  # 委托


def execute(request_id: str) -> dict[str, Any]:  # 批准后执行（同步壳）
    return asyncio.run(approval_service.execute_approved(request_id))  # 委托


def expire() -> list[str]:  # 超时扫描
    return approval_service.expire_all()  # 委托