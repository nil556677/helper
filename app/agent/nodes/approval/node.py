"""路由级确认节点：需确认 → 聊天弹框(确认/拒绝) → 幂等 decide → 执行/停止。

跨重启正确性：pending 在 `approval_requests`（幂等去重）里；用户不选就关应用，
记录保持 pending；下次同对话同参数再触发时去重返回同一 request_id，弹框仍正确显示。
触发即**告知**（要查/要做什么）+ 用户**确认** + **留痕**（approval_request + approval_audit）。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from langgraph.types import interrupt  # 暂停待人工决策

from app.agent.approval import repository as approval_repo  # 确认仓储
from app.agent.approval.repository import ApprovalConflict  # 乐观并发冲突
from app.agent.react.graph import CONFIDENTIAL_NOTICE  # 保密提示（附注）

__all__ = ["approval_hitl_node", "decide_and_execute"]  # 导出


def decide_and_execute(  # 幂等决策 + 执行（可单测）
    request_id: str,  # 请求
    action: str,  # approve / reject
) -> dict[str, Any]:  # 结果
    """按 pending 乐观更新；approve 且未执行过 → 执行工具；reject → 停。
    崩溃恢复：已 approve 但未 executed（确认后崩在执行前）→ 恢复时仍补执行（幂等，不重复）。"""
    import asyncio  # 跑异步

    if action not in ("approve", "reject"):  # 非法值
        return {"ok": False, "reason": "invalid_action"}  # 忽略
    try:  # 乐观决策（幂等：非 pending 会 Conflict → 说明已处理/超时）
        approval_repo.decide(request_id, "approved" if action == "approve" else "rejected",
                             decided_by="user")  # 决策
    except ApprovalConflict:  # 已处理/超时
        # 已 approve 但还没 executed（此前确认后崩在 execute 之前）→ 落到执行补跑（幂等）
        row = approval_repo.get_request(request_id)  # 读记录
        approved_unexecuted = (  # 是否"已批准未执行"
            action == "approve"  # 用户方向是确认
            and row is not None  # 记录在
            and str(row.get("status") or "") == "approved"  # 已批准
            and not approval_repo.has_executed(request_id)  # 未执行过
        )  # 结束
        if not approved_unexecuted:  # 其余已处理场景
            return {"ok": False, "reason": "already_decided"}  # 不再执行
    except Exception:  # 其它异常
        return {"ok": False, "reason": "already_decided"}  # 不再执行
    if action == "reject":  # 拒绝
        return {"ok": True, "executed": False, "note": "已拒绝，未执行/未查询。"}  # 停
    from app.agent.approval import service as approval_service  # 确认服务

    out = asyncio.run(approval_service.execute_approved(request_id))  # 执行（幂等）
    return {"ok": True, "executed": bool(out.get("executed")), "note": out.get("reason") or "", "data": out.get("data")}  # 结果


def approval_hitl_node(state: dict[str, Any]) -> dict[str, Any]:  # 图节点
    """有 pending → 弹框；空 → 直通。数据不进聊天正文，审计是结构化行。"""
    pending = list(state.get("approval_pending") or [])  # 待审元信息
    if not pending:  # 无待审
        return {}  # 直通
    first = pending[0] if isinstance(pending[0], dict) else {}  # 当前一条
    rid = str(first.get("request_id") or "")  # 请求 id
    summary = str(first.get("summary") or "查看订单")  # 待确认内容
    # 弹框（中断；聊天卡 kind=approve 渲染 确认/拒绝）
    value = interrupt({  # 暂停等待用户确认
        "kind": "approve",  # 类型
        "title": "确认执行",  # 标题
        "description": f"{summary}\n{CONFIDENTIAL_NOTICE}",  # 告知内容 + 保密提示（提示仅附注）
        "options": [  # 仅确认/拒绝两个明确选择，避免"人打的算不算确认"歧义
            {"value": "approve", "label": "确认执行"},  # 确认
            {"value": "reject", "label": "拒绝"},  # 拒绝
        ],  # 结束
        "request_ids": [p.get("request_id", "") for p in pending if isinstance(p, dict)],  # 请求 id 列表
    })  # 结束
    action = str(value.get("action") if isinstance(value, dict) else value)  # 恢复值（chip value，兼容对象）
    if action not in ("approve", "reject"):  # 非法/未知（含自由输入）
        action = "reject"  # 保守：不执行
    out = decide_and_execute(rid, action)  # 幂等决策 + 执行
    answer = (  # 回复
        f"{summary}\n已确认并执行，已留痕。" if (out.get("executed")) else  # 已确认执行
        (f"{summary}\n已拒绝，未执行/未查询。" if action == "reject" else "已处理。")  # 拒绝/其它
    )  # 结束
    return {  # 写回
        "answer": answer,  # 回答
        "note": out.get("note") or "",  # 说明
        "approval_pending": [],  # 已处理，清空标识
        "fallback_used": True,  # 走对话确认路径
    }  # 结束
