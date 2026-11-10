"""设置 · 审批台账（只读）：列出审批记录与状态，决策只在聊天框。

读写（create/decide/execute）保留给聊天 HITL 用；本页不落任何决策。
"""

from __future__ import annotations  # 延迟值注解

import time  # 时间格式化

from nicegui import ui  # UI

from app.services import approvals as approvals_service  # 审批服务（只读台账）

__all__ = ["render"]  # 导出


_STATUS = {  # 状态文案
    "pending": ("待确认", "bg-amber-500"),  # 待确认
    "approved": ("已确认", "bg-green-600"),  # 已确认
    "rejected": ("已拒绝", "bg-red-600"),  # 已拒绝
    "expired": ("已超时", "bg-grey-600"),  # 已超时
}  # 状态文案结束


def _human(ts) -> str:  # 时间
    try:  # 浮点秒
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts))) if ts else "—"  # 时间
    except Exception:  # 坏值
        return "—"  # 占位


def render() -> None:  # 画分区
    """审批台账（只读）：审批记录 + 刷新；不提供决定操作（决定在聊天框）。"""
    with ui.column().classes("w-full gap-3"):  # 容器
        def _rebuild() -> None:  # 重建台账
            box.clear()  # 清
            with box:  # 重建
                rows = approvals_service.ledger()  # 台账
                if not rows:  # 空
                    ui.label("暂无审批记录").classes("hp-field-help")  # 提示
                for item in rows:  # 逐条
                    status = str(item.get("status") or "unknown")  # 状态
                    st_label, st_cls = _STATUS.get(status, ("—", "bg-grey-600"))  # 文案+色
                    with ui.row().classes("w-full items-center gap-2 no-wrap px-2 py-1"):  # 一行
                        ui.label(str(item.get("tool_name") or "")).classes("hp-field-help").style("width:110px;")  # 工具
                        summary = str(item.get("resource_summary") or "")  # 审核内容
                        ui.label(summary).classes("flex-1 hp-field-help").style("min-width:0; ellipsis:1;")  # 内容
                        ui.label(_human(item.get("created_at"))).classes("hp-field-help").style("width:96px;")  # 时间
                        ui.badge(st_label, color=st_cls).props("outline")  # 状态徽标

        box = ui.column().classes("w-full gap-1 hp-card px-3 py-3")  # 台账容器
        ui.button("刷新", icon="refresh", on_click=_rebuild).props("dense outline")  # 刷新
        with ui.row().classes("w-full"):  # 说明
            ui.label("决策在聊天框进行（确认/拒绝）；这里只读留痕，不提供操作。").classes("hp-hint")  # 说明
        _rebuild()  # 首帧