"""审批服务：列表/决策/超时扫描/批准后执行（薄封装仓储，供 API/UI/定时任务调用）。"""

from __future__ import annotations  # 延迟值注解

import json  # 解析参数
from typing import Any  # 任意值

from app.agent.approval import repository  # 仓储

__all__ = ["decide", "execute_approved", "expire_all", "pending"]  # 导出


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


async def execute_approved(request_id: str, *, registry: dict[str, Any] | None = None) -> dict[str, Any]:  # 批准后执行
    """仅 approved 且未执行过才执行工具（幂等）；拒绝/超时/已执行 → 不执行。"""
    from app.agent.tools.catalog import tool_catalog  # 工具目录
    from app.agent.tools.wiring import unwrap_tool_output  # 还原包络

    row = repository.get_request(request_id)  # 取请求
    if row is None:  # 不存在
        return {"executed": False, "reason": "not_found"}  # 返回
    if str(row.get("status") or "") != "approved":  # 非批准
        return {"executed": False, "reason": str(row.get("status") or "")}  # 不执行
    if repository.has_executed(request_id):  # 已执行过（幂等）
        return {"executed": False, "reason": "already"}  # 不重跑
    tools = registry if registry is not None else tool_catalog()  # 工具表
    tool = tools.get(str(row.get("tool_name") or ""))  # 取工具
    if tool is None:  # 工具不存在
        return {"executed": False, "reason": "tool_missing"}  # 返回
    try:  # 执行
        args = json.loads(str(row.get("args_redacted") or "{}"))  # 参数
        envelope = await tool.ainvoke(args)  # 调工具
        out = unwrap_tool_output(envelope)  # 还原
        repository.insert_audit(request_id, "executed", "system", {"ok": bool(getattr(out, "ok", False))})  # 审计
        return {"executed": True, "ok": bool(getattr(out, "ok", False)), "data": getattr(out, "data", None)}  # 结果
    except Exception as exc:  # 失败
        repository.insert_audit(request_id, "execute_failed", "system", {"error": str(exc)})  # 审计
        return {"executed": False, "reason": str(exc)}  # 返回