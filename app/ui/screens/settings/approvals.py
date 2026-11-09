"""设置 · 人工审批：待审请求列表 + 批准/拒绝/执行 + 刷新（调 `services/approvals`）。"""

from __future__ import annotations  # 延迟值注解

from nicegui import ui  # UI

from app.services import approvals as approvals_service  # 审批服务（对外）

__all__ = ["render"]  # 导出


def render() -> None:  # 画分区
    """待审请求 + 审批操作。有审批才展示（无数据不占面积）。"""
    with ui.column().classes("w-full gap-3"):  # 容器
        def _rebuild() -> None:  # 重建列表
            box.clear()  # 清
            with box:  # 重建
                ui.label("待人工审批的请求").classes("hp-col-name")  # 标题
                rows = approvals_service.pending()  # 待审
                if not rows:  # 空
                    ui.label("暂无待审批请求").classes("hp-field-help")  # 提示
                for item in rows:  # 逐条
                    rid = str(item.get("request_id") or "")  # 请求 id
                    with ui.row().classes("w-full items-center gap-2 no-wrap px-2 py-1"):  # 一行
                        summary = f"[{item.get('tool_name')}] {item.get('resource_summary')}"  # 摘要
                        ui.label(summary).classes("flex-1 hp-field-help").style("min-width:0;")  # 摘要
                        ui.button("批准", on_click=lambda rid=rid: _handle(rid, "approved"))  # 批准
                        ui.button("拒绝", on_click=lambda rid=rid: _handle(rid, "rejected"))  # 拒绝
                        ui.button("执行", color="secondary", on_click=lambda rid=rid: _execute(rid))  # 执行

        def _handle(rid: str, action: str) -> None:  # 批准/拒绝
            try:  # 决策
                approvals_service.decide(rid, action)  # 决策
                ui.notify(f"已{ '批准' if action == 'approved' else '拒绝' } {rid[:8]}", type="positive")  # 提示
            except Exception as exc:  # 冲突/超时
                ui.notify(f"处理失败：{exc}", type="negative")  # 提示
            _rebuild()  # 刷

        def _execute(rid: str) -> None:  # 执行
            out = approvals_service.execute(rid)  # 执行
            if out.get("executed"):  # 成功
                ui.notify(f"已执行 {rid[:8]}", type="positive")  # 提示
            else:  # 未执行
                ui.notify(f"未执行（{out.get('reason')}）", type="warning")  # 提示
            _rebuild()  # 刷

        box = ui.column().classes("w-full gap-1 hp-card px-3 py-3")  # 列表容器
        ui.button("刷新", icon="refresh", on_click=_rebuild).props("dense outline")  # 刷新
        _rebuild()  # 首帧