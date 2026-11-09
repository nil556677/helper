"""审批服务：列表/决策/超时扫描（薄封装仓储，供 API/UI/定时任务调用）。"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from app.agent.approval import repository  # 仓储

__all__ = ["decide", "expire_all", "pending"]  # 导出


def pending(tenant_id: str, *, limit: int = 50) -> list[dict[str, Any]]:  # 待审列表
    """按租户取 pending（新的在前）。"""
    return repository.list_pending(tenant_id, limit=limit)  # 委托


def decide(request_id: str, action: str, *, decided_by: str, note: str = "") -> None:  # 决策
    """批准/拒绝（乐观并发；已被处理会抛 ApprovalConflict）。"""
    if action not in ("approved", "rejected"):  # 只允许两种
        raise ValueError("action 只能是 approved 或 rejected")  # 拒绝非法
    repository.decide(request_id, action, decided_by, note)  # 委托


def expire_all() -> list[str]:  # 超时扫描
    """把过期 pending 标 expired，返回需唤醒的 request_id。"""
    return repository.expire_pending()  # 委托