"""聊天页的实时视图（组头、过程组、工具、审批、检索依据）。"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from app.agent.contracts import GROUP_LABELS, GROUP_ORDER, StageGroup
from app.services import settings as settings_service
from app.ui.screens.chat.dialogs import _open_settings_dialog
from app.ui.screens.chat.state import _GROUP_KEYS, _alive, _LiveState
from app.ui.screens.chat.widgets import _render_basis_content  # 依据正文（历史/实时共用）


def _build_live(live_state: _LiveState, box: Any, scroll: Any = None) -> dict[str, Any]:  # 建实时视图
    """标准档：组头 + 可展开过程 + 检索依据 + 答案。"""
    view: dict[str, Any] = {  # 视图状态
        "box": box,  # 容器
        "scroll": scroll,  # 滚动区（流式时自动跟到最后一行）
        "scrolled_at": 0.0,  # 上次自动滚动的时刻（节流）
        "busy": None,  # 运行中的转圈图标
        "headline": None,  # 组头文字
        "meta": None,  # 组头右侧
        "chevron": None,  # 展开符号
        "details": None,  # 过程明细
        "details_open": False,  # 是否展开
        "basis_row": None,  # 检索依据行
        "basis_label": None,  # 依据摘要
        "basis_box": None,  # 依据正文
        "basis_open": False,  # 依据是否展开
        "answer_md": None,  # 答案
        "note_label": None,  # 说明
        "conflict_label": None,  # 资料冲突提示
        "citations_box": None,  # 引用
        "usage": None,  # 用量
        "groups": {},  # 分组状态
        "basis": {},  # 检索依据
        "settings": {},  # 本轮设置
        "answer_text": "",  # 已生成文本
        "streaming": False,  # 是否还在出字（末尾显示光标）
        "answer_at": 0.0,  # 上次把文本画进 markdown 的时刻
        "answer_dirty": False,  # 有新文本还没画
        "citations": [],  # 引用
        "note": "",  # 说明
        "note_kind": "plain",  # 说明样式
        "retry_note": "",  # 重试提示（只留最新一条）
        "conflict": "",  # 冲突提示
        "run_state": "",  # 运行状态
        "total_ms": 0,  # 总耗时
        "ttft_ms": 0,  # 首字耗时
    }  # 视图结束
    if box is None or box.is_deleted:  # 不在当前页
        return view  # 只收集数据
    box.clear()  # 清掉等待行
    with box:  # 重建
        header = ui.row().classes("w-full items-center gap-2 no-wrap hp-process-row")  # 组头
        with header:  # 组头内容
            ui.icon("travel_explore").classes("text-base hp-process-icon")  # 图标
            view["busy"] = ui.icon("autorenew").classes(  # 运行中：转圈，让用户知道没卡住
                "text-base hp-process-icon animate-spin"
            )  # 转圈结束
            view["headline"] = ui.label("已收到，正在处理…").classes("hp-process-title")  # 文案
            view["meta"] = ui.label("").classes("hp-field-help")  # 右侧
            ui.space()  # 撑开
            view["chevron"] = ui.label("▸").classes("hp-field-help")  # 展开符号
        header.on("click", lambda _e: _toggle_details(view))  # 点击展开
        view["details"] = ui.column().classes("w-full gap-1 pl-6")  # 过程明细
        view["details"].visible = False  # 默认收起
        view["approval_box"] = ui.column().classes("w-full gap-2")  # 审批区
        view["tools"] = []  # 工具行
        view["reasoning_row"] = ui.element("details").classes("w-full hp-reasoning")  # 思考
        with view["reasoning_row"]:  # 思考内容
            with ui.element("summary").classes("w-full items-center gap-2 no-wrap hp-reasoning-row"):  # 头
                ui.icon("psychology").classes("text-sm hp-reasoning-icon")  # 图标
                ui.label("思考过程").classes("hp-reasoning-title")  # 标题
                view["reasoning_preview"] = ui.label("").classes("hp-hint hp-reasoning-preview")  # 预览
            view["reasoning_md"] = ui.markdown("").classes("hp-md-compact")  # Markdown
        view["reasoning_row"].visible = False  # 有 reasoning 才显示
        view["reasoning_text"] = ""  # 已收到的思考
        view["reasoning_at"] = 0.0  # 上次画思考的时刻
        view["reasoning_dirty"] = False  # 有新思考还没画
        basis_row = ui.row().classes("w-full items-center gap-2 no-wrap hp-basis-row")  # 依据头
        with basis_row:  # 依据头内容
            ui.icon("manage_search").classes("text-sm hp-hint")  # 图标
            ui.label("检索依据").classes("hp-field-name")  # 标题
            view["basis_label"] = ui.label("").classes("hp-field-help")  # 摘要
            ui.space()  # 撑开
            view["basis_chevron"] = ui.label("▸").classes("hp-field-help")  # 箭头
        basis_row.on("click", lambda _e: _toggle_basis(view))  # 点击展开
        view["basis_row"] = basis_row  # 记录
        view["basis_row"].visible = False  # 默认隐藏
        view["basis_box"] = ui.column().classes("w-full gap-1 pl-6 hp-basis-body")  # 依据正文
        view["basis_box"].visible = False  # 默认收起
        view["answer_md"] = ui.markdown("").classes("hp-markdown w-full")  # 答案
        view["note_label"] = ui.label("").classes("hp-hint")  # 说明
        view["conflict_label"] = ui.label("").classes("hp-note-warning")  # 冲突
        view["conflict_label"].visible = False  # 默认隐藏
        view["citations_box"] = ui.column().classes("w-full")  # 引用
        with ui.row().classes("w-full items-center gap-2 no-wrap pt-1"):  # 底部
            view["usage"] = ui.label("").classes("hp-field-help")  # 用量
            ui.space()  # 撑开
            if settings_service.get_log_debug():  # 本轮设置是调试信息：只在调试模式展示
                ui.button("本轮设置", on_click=lambda: _open_settings_dialog(  # 设置按钮
                    view["settings"],  # 基础设置
                    usage=view.get("usage_data"),  # token 用量
                    duration_ms=view.get("total_ms"),  # 总耗时
                    ttft_ms=view.get("ttft_ms"),  # 首字耗时
                )).props("flat dense no-caps").classes("hp-field-help")  # 弱化样式
    live_state.views.append(view)  # 注册（本页自己管，不跨窗口）
    return view  # 返回


def _toggle_details(view: dict[str, Any]) -> None:  # 展开/收起过程
    """标准档默认收起，点击组头切换。"""
    if not _alive(view) or view["details"] is None:  # 没 UI
        return  # 跳过
    view["details_open"] = not view["details_open"]  # 切换
    view["details"].visible = view["details_open"]  # 应用
    if view["chevron"] is not None:  # 有箭头
        view["chevron"].text = "▾" if view["details_open"] else "▸"  # 更新
    view["details"].update()  # 刷新
    if view["chevron"] is not None:  # 刷新箭头
        view["chevron"].update()  # 更新


def _set_details(view: dict[str, Any], open_: bool) -> None:  # 强制展开
    """失败/停止时自动展开。"""
    if view["details_open"] == open_:  # 已是目标
        return  # 跳过
    _toggle_details(view)  # 切换一次


def _toggle_basis(view: dict[str, Any]) -> None:  # 展开/收起依据
    """检索依据单独折叠。"""
    if not _alive(view) or view["basis_box"] is None:  # 没 UI
        return  # 跳过
    view["basis_open"] = not view["basis_open"]  # 切换
    view["basis_box"].visible = view["basis_open"]  # 应用
    view["basis_chevron"].text = "▾" if view["basis_open"] else "▸"  # 箭头
    view["basis_box"].update()  # 刷新


def _group_status(stages: dict[str, dict[str, Any]]) -> str:  # 组状态
    """running 优先，其次 failed，最后 done。"""
    statuses = [str(item.get("status") or "") for item in stages.values()]  # 全部
    if "running" in statuses:  # 运行中
        return "running"  # 运行
    if "failed" in statuses:  # 失败
        return "failed"  # 失败
    if statuses and all(status in ("done", "skipped") for status in statuses):  # 全完成
        return "done"  # 完成
    return "pending"  # 其他


def _update_group(  # 更新分组
    view: dict[str, Any],  # 视图
    stage_id: str,  # 步骤 id（只在组内做原地更新的键）
    group: str,  # 分组（事件自带，不再按 id 查表）
    label: str,  # 本次步骤的文案
    status: str,  # 状态
    detail: str = "",  # 明细
    ms: int | None = None,  # 耗时
) -> None:  # 无返回
    """把内部步骤按事件自带的分组合并成用户能懂的一行。"""
    if group not in _GROUP_KEYS:  # 没有分组 / 只埋 trace 的步骤
        return  # 不展示
    group_state = view["groups"].setdefault(  # 取/建这一组
        group,  # 分组
        {"label": GROUP_LABELS[StageGroup(group)], "stages": {}},  # 组名 + 组内步骤
    )  # 取组结束
    group_state["stages"][stage_id] = {"label": label, "status": status, "detail": detail, "ms": ms}  # 记录
    group_state["status"] = _group_status(group_state["stages"])  # 状态
    group_state["ms"] = sum(int(item.get("ms") or 0) for item in group_state["stages"].values())  # 耗时
    details: list[str] = []  # 明细
    for item in group_state["stages"].values():  # 逐项
        text = str(item.get("detail") or "").strip()  # 明细
        if text and text not in details:  # 去重
            details.append(text)  # 追加
    group_state["detail"] = "；".join(details[:3])  # 最多三条
    _render_groups(view)  # 重画分组
    _refresh_header(view)  # 更新组头


def _render_groups(view: dict[str, Any]) -> None:  # 画分组
    """只画用户级步骤，不画内部 stage。"""
    if view["details"] is None or not _alive(view):  # 没 UI
        return  # 跳过
    view["details"].clear()  # 清空
    with view["details"]:  # 画
        for group_key in GROUP_ORDER:  # 固定顺序
            group = view["groups"].get(group_key.value)  # 组
            if group is None:  # 没有
                continue  # 跳过
            mark_map = {"running": "…", "done": "✓", "failed": "✗", "pending": "·"}  # 符号
            with ui.row().classes("w-full items-center gap-2 no-wrap"):  # 行
                ui.label(mark_map.get(group["status"], "·")).classes("hp-field-name").style("width: 18px;")  # 符号
                ui.label(group["label"]).classes("hp-field-name")  # 名称
                ui.label(group.get("detail") or "").classes("hp-field-help")  # 明细
                ui.space()  # 撑开
                ms = int(group.get("ms") or 0)  # 耗时
                if ms:  # 有耗时
                    ui.label(f"{ms}ms").classes("hp-field-help")  # 显示
        for tool in view["tools"]:  # 工具行
            _render_tool_row(tool)  # 画
        if view.get("retry_note"):  # 有重试提示
            with ui.row().classes("w-full items-center gap-2 no-wrap"):  # 一行
                ui.icon("autorenew").classes("text-sm hp-hint")  # 图标
                ui.label(view["retry_note"]).classes("hp-hint")  # 文案
    view["details"].update()  # 刷新


def _render_tool_row(tool: dict[str, Any]) -> None:  # 工具行
    """读取/写入/编辑/命令：图标 + 人话 + 路径/明细。"""
    variant = str(tool.get("variant") or "")  # 类型
    icons = {"read": "menu_book", "write": "note_add", "edit": "edit", "bash": "terminal"}  # 图标
    titles = {"read": "读取文件", "write": "写入文件", "edit": "编辑文件", "bash": "运行命令"}  # 标题
    status = str(tool.get("status") or "done")  # 状态
    marks = {"running": "…", "done": "✓", "failed": "✗", "stopped": "·"}  # 符号
    with ui.row().classes("w-full items-center gap-2 no-wrap"):  # 行
        ui.icon(icons.get(variant, "build")).classes("text-sm hp-field-name")  # 图标
        ui.label(titles.get(variant, str(tool.get("name") or "工具"))).classes("hp-field-name")  # 标题
        path = str(tool.get("path") or "")  # 文件
        if path:  # 有路径
            ui.label(path).classes("hp-field-help").style("overflow:hidden;text-overflow:ellipsis")  # 路径
        detail = str(tool.get("detail") or "")  # 明细
        if detail:  # 有明细
            ui.label(detail).classes("hp-field-help")  # 明细
        ui.space()  # 撑开
        ui.label(marks.get(status, "·")).classes("hp-field-help")  # 状态
    if tool.get("diff"):  # 有改动
        with ui.expansion("查看改动").classes("w-full hp-step"):  # 折叠
            ui.code(str(tool["diff"])).classes("w-full")  # diff


def _render_approval(view: dict[str, Any], data: dict[str, Any]) -> None:  # 审批卡
    """写文件/改文件/跑命令前确认；始终展开。"""
    box = view.get("approval_box")  # 容器
    if box is None or not _alive(view):  # 没 UI
        return  # 跳过
    request_id = str(data.get("request_id") or "")  # id
    box.clear()  # 清旧
    with box, ui.card().classes("w-full hp-card gap-2"):  # 画
        ui.label("需要你确认").classes("hp-card-title")  # 标题
        ui.label(f"{data.get('name') or '操作'}：{data.get('detail') or ''}").classes("hp-field-help")  # 明细
        if data.get("risk"):  # 风险
            ui.label(str(data["risk"])).classes("hp-note-error")  # 提示
        if data.get("diff"):  # 改动
            with ui.expansion("查看改动").classes("w-full hp-step"):  # 折叠
                ui.code(str(data["diff"])).classes("w-full")  # diff
        with ui.row().classes("w-full justify-end gap-2"):  # 按钮
            ui.button("拒绝", on_click=lambda: _decide(view, request_id, False)).props("flat")  # 拒绝
            ui.button("允许一次", on_click=lambda: _decide(view, request_id, True)).props("unelevated")  # 允许


def _decide(view: dict[str, Any], request_id: str, allow: bool) -> None:  # 决定
    """把决定交回后台；清掉卡。"""
    handle = view.get("handle")  # 流句柄
    if handle is not None:  # 有
        handle.approve(request_id, allow)  # 交回
    box = view.get("approval_box")  # 容器
    if box is not None and not box.is_deleted:  # 还在
        box.clear()  # 清空
        box.update()  # 刷新


def _meta_text(view: dict[str, Any]) -> str:  # 组头右侧
    """引用数 + 总耗时。"""
    parts: list[str] = []  # 片段
    citations = len(view["citations"])  # 引用
    if citations:  # 有引用
        parts.append(f"{citations} 段引用")  # 引用数
    if view["total_ms"]:  # 有耗时
        parts.append(f"{view['total_ms'] / 1000:.1f}s")  # 秒
    return " · ".join(parts)  # 拼接


def _refresh_header(view: dict[str, Any]) -> None:  # 更新组头
    """运行中显示“正在查找资料”，结束显示“已…”，并用转圈图标表示还活着。"""
    if view["headline"] is None or not _alive(view):  # 没 UI
        return  # 跳过
    running = next((g for g in view["groups"].values() if g.get("status") == "running"), None)  # 运行中
    failed = any(g.get("status") == "failed" for g in view["groups"].values())  # 失败
    if view["run_state"] == "error" or failed:  # 失败
        text = "本轮失败"  # 文案
    elif view["run_state"] == "cancelled":  # 停止
        text = "已停止生成"  # 文案
    elif view["run_state"] == "cancelling":  # 正在停止（已点停止、还没收到终态）
        text = "正在停止…"  # 文案
    elif running is not None:  # 运行中
        text = f"正在{running['label']}…"  # 文案
    elif view["groups"].get(StageGroup.GENERATE.value) is not None:  # 生成过
        text = "已查找资料并生成回答"  # 文案
    elif view["groups"].get(StageGroup.SEARCH.value) is not None:  # 查过
        text = "已查找资料"  # 文案
    elif view["run_state"] in ("context_loading", "retrieving"):  # 准备/检索
        text = "正在准备…"  # 文案
    elif view["run_state"] == "done":  # 完成
        text = "已完成"  # 文案
    else:  # 初始
        text = "已收到，正在处理…"  # 文案
    view["headline"].text = text  # 写
    view["meta"].text = _meta_text(view)  # 右侧
    view["headline"].update()  # 刷新
    view["meta"].update()  # 刷新
    alive = view["run_state"] not in ("done", "error", "cancelled")  # 只要没到终态就一直转
    if view["busy"] is not None:  # 有图标
        view["busy"].visible = alive  # 跑着才转
        view["busy"].update()  # 刷新


def _render_basis(view: dict[str, Any]) -> None:  # 画检索依据
    """关键词/计数/来源，全部用户可读。"""
    basis = view["basis"]  # 依据
    if not basis:  # 没有
        return  # 跳过
    if view["basis_row"] is not None and _alive(view):  # 有 UI
        view["basis_row"].visible = True  # 显示
        counts = basis.get("hit_counts") or {}  # 计数
        view["basis_label"].text = f"命中 {counts.get('selected', 0)} 段资料"  # 摘要
        view["basis_label"].update()  # 刷新
        view["basis_row"].update()  # 刷新
    if view["basis_box"] is None or not _alive(view):  # 没 UI
        return  # 跳过
    view["basis_box"].clear()  # 清空
    with view["basis_box"]:  # 画（与历史共用同一份渲染，避免双份实现漂移）
        _render_basis_content(basis)  # 关键词/计数/来源（单一实现）
    view["basis_box"].update()  # 刷新
