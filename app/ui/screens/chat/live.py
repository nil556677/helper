"""聊天页的实时视图（组头、过程组、工具、审批、检索依据）。"""

from __future__ import annotations

import contextlib  # 停表容错
import time  # 实时耗时
from typing import Any  # 任意值

from nicegui import ui

from app.services import settings as settings_service
from app.ui.screens.chat.dialogs import _open_settings_dialog
from app.ui.screens.chat.scroll import _schedule_scroll
from app.ui.screens.chat.state import _alive, _LiveState
from app.ui.screens.chat.widgets import _render_basis_content, _render_steps_flow  # 依据/时间线（历史实时共用）


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
        "groups": {},  # 兼容旧字段（新用 steps）
        "steps": [],  # 有序步骤（时间线单一事实源）
        "step_ids": {},  # step_id → 索引（原地更新）
        "basis": {},  # 检索依据
        "settings": {},  # 本轮设置
        "answer_text": "",  # 已生成文本
        "streaming": False,  # 是否还在出字（末尾显示光标）
        "answer_at": 0.0,  # 上次把文本画进 markdown 的时刻
        "answer_dirty": False,  # 有新文本还没画
        "answer_shown": None,  # 打字揭示游标（None=全显；DELTA 起打字）
        "_typewriter_task": None,  # 打字后台任务（终态取消）
        "citations": [],  # 引用
        "note": "",  # 说明
        "note_kind": "plain",  # 说明样式
        "retry_note": "",  # 重试提示（只留最新一条）
        "conflict": "",  # 冲突提示
        "run_state": "",  # 运行状态
        "total_ms": 0,  # 总耗时
        "ttft_ms": 0,  # 首字耗时
        "started_at": time.perf_counter(),  # 本轮开始时刻（算实时耗时）
        "ticker": None,  # 实时耗时的刷新计时器
    }  # 视图结束
    if box is None or box.is_deleted:  # 不在当前页
        return view  # 只收集数据
    box.clear()  # 清掉等待行
    with box:  # 重建
        header = ui.row().classes("w-full items-center gap-2 no-wrap hp-process-row")  # 组头
        with header:  # 组头内容
            view["busy"] = ui.icon("autorenew").classes(  # 运行中：转圈，让用户知道没卡住
                "text-base hp-process-icon animate-spin"
            )  # 转圈结束
            view["headline"] = ui.label("正在思考中···").classes("hp-process-title")  # 文案
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
    view["ticker"] = ui.timer(0.5, lambda: _refresh_header(view))  # 每 0.5s 刷新"正在…耗时"
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


def _steps_status(steps: list[dict[str, Any]]) -> str:  # 整体状态
    """running 优先，其次 failed，最后 done/pending。"""
    statuses = [str(item.get("status") or "") for item in steps]  # 全部
    if "running" in statuses:  # 运行中
        return "running"  # 运行
    if "failed" in statuses:  # 失败
        return "failed"  # 失败
    if statuses and all(s in ("done", "skipped") for s in statuses):  # 全完成
        return "done"  # 完成
    return "pending"  # 其他


def _upsert_step(  # 新增/更新一个时间线步骤（单一事实源）
    view: dict[str, Any],  # 视图
    step_id: str,  # 步骤 id（stage_id / 工具 call_id）
    *,  # 关键字
    group: str,  # 分组（决定图标）
    title: str,  # 人话标题
    status: str,  # 状态
    detail: str = "",  # 明细（一行摘要）
    body: str = "",  # 点击展开后的完整内容
    ms: int | None = None,  # 耗时
    children: list[dict] | None = None,  # 子步骤（工具参数/结果）
) -> None:  # 无返回
    """按 step_id 原地更新或追加；再重画时间线并（运行中）滚到底。"""
    steps = view["steps"]  # 有序步骤
    index = view["step_ids"].get(step_id)  # 已有索引
    if index is None:  # 新增
        steps.append({  # 追加到末尾
            "id": step_id,  # id
            "group": group,  # 分组
            "title": title,  # 标题
            "status": status,  # 状态
            "detail": detail,  # 明细
            "body": body,  # 展开正文
            "ms": int(ms or 0),  # 耗时
            "children": children or [],  # 子项
        })  # 追加结束
        view["step_ids"][step_id] = len(steps) - 1  # 记索引
    else:  # 已有：原地更新
        item = steps[index]  # 取
        item.update(  # 合并
            group=group,  # 分组
            title=title,  # 标题
            status=status,  # 状态
            detail=detail,  # 明细
        )  # 合并结束
        if body:  # 有正文才覆盖（running 帧可能空）
            item["body"] = body  # 展开正文
        if ms is not None:  # 只给就更新（不改"未知"）
            item["ms"] = int(ms)  # 耗时
        if children:  # 有子项
            item["children"] = children  # 覆盖子项
    _render_steps(view)  # 重画
    _refresh_header(view)  # 组头
    _maybe_scroll(view)  # 运行中滚到底


def _maybe_scroll(view: dict[str, Any]) -> None:  # 运行中自动滚到底
    """有滚动区且还在跑，就滚到最新一步（节流，别每步都刷）。"""
    if not (view.get("scroll") and view.get("run_state") not in ("done", "error", "cancelled", "waiting")):  # 不在跑/没滚动区
        return  # 跳过
    now = time.perf_counter()  # 当前时刻
    if now - view.get("scrolled_at", 0.0) < 0.15:  # 节流 150ms
        return  # 跳过
    view["scrolled_at"] = now  # 记时刻
    _schedule_scroll(view["scroll"])  # 排一次延迟滚动


def _render_steps(view: dict[str, Any]) -> None:  # 画时间线（实时）
    """把有序步骤画进 details 区；tools 已并入步骤（工具调用成为子步骤/独立步骤）。"""
    if view["details"] is None or not _alive(view):  # 没 UI
        return  # 跳过
    view["details"].clear()  # 清空
    with view["details"]:  # 画
        _render_steps_flow(view["steps"])  # 单一时间线渲染器（实时）
        if view.get("retry_note"):  # 有重试提示
            with ui.row().classes("w-full items-center gap-2 no-wrap"):  # 一行
                ui.icon("autorenew").classes("text-sm hp-hint")  # 图标
                ui.label(view["retry_note"]).classes("hp-hint")  # 文案
    view["details"].update()  # 刷新


def _render_approval(view: dict[str, Any], data: dict[str, Any]) -> None:  # 确认卡
    """查敏感/改数据前请用户确认；始终展开。"""
    box = view.get("approval_box")  # 容器
    if box is None or not _alive(view):  # 没 UI
        return  # 跳过
    request_id = str(data.get("request_id") or "")  # id
    box.clear()  # 清旧
    with box, ui.card().classes("w-full hp-card gap-2"):  # 画
        ui.label("需要你确认").classes("hp-card-title")  # 标题
        ui.label(f"{data.get('name') or '操作'}：{data.get('detail') or ''}").classes("hp-field-help")  # 告知明细
        if data.get("risk"):  # 风险
            ui.label(str(data["risk"])).classes("hp-note-error")  # 提示
        if data.get("diff"):  # 改动
            with ui.expansion("查看改动").classes("w-full hp-step"):  # 折叠
                ui.code(str(data["diff"])).classes("w-full")  # diff
        with ui.row().classes("w-full justify-end gap-2"):  # 按钮
            ui.button("拒绝", on_click=lambda: _decide(view, request_id, False)).props("flat")  # 拒绝
            ui.button("确认执行", on_click=lambda: _decide(view, request_id, True)).props("unelevated")  # 确认


def _render_clarify(view: dict[str, Any], data: dict[str, Any]) -> None:  # 澄清卡
    """HITL 暂停：借鉴成熟聊天 UX（ChatGPT suggestion chips / CopilotKit interrupt）——
    紧凑卡片 + 横向快捷 chips，点选即发；自由输入走输入框。"""
    box = view.get("approval_box")  # 容器
    if box is None or not _alive(view):  # 没 UI
        return  # 跳过
    options = [item for item in (data.get("options") or []) if isinstance(item, dict)]  # 选项
    kind = str(data.get("kind") or "choose")  # choose=选路由 / input=补参数
    box.clear()  # 清旧
    with box, ui.card().classes("w-full hp-card gap-2 py-3").style(  # 紧凑卡 + 左侧强调条
        "border-left: 3px solid var(--hp-accent);"
    ):  # 画
        with ui.row().classes("w-full items-center gap-2"):  # 标题行
            _icon = "verified_user" if kind == "approve" else ("contact_support" if kind == "input" else "quiz")  # 图标按类型
            ui.icon(_icon).classes("text-amber-500")  # 图标
            ui.label(str(data.get("title") or "需要确认")).classes("hp-card-title")  # 标题
        if data.get("description"):  # 追问/说明（补参数时这就是问题本身）
            ui.label(str(data["description"])).classes("text-sm opacity-80")  # 轻量正文
        if options and kind != "input":  # 快捷 chips：仅"选路由"场景；补参数=让用户填，不列选项
            with ui.row().classes("w-full flex-wrap gap-2"):  # chips 区
                for item in options:  # 逐项
                    value = str(item.get("value") or "")  # 值
                    label = str(item.get("label") or value)  # 文案
                    ui.button(  # chip 式按钮
                        label,  # 文案
                        on_click=lambda v=value: _choose(view, v),  # 点选即回
                    ).props("outline no-caps size=md unelevated").classes("rounded-full border-2")  # 圆角描边
        if kind == "input":  # 补参数：让用户"填"，内嵌一个输入框+确定（回车也行）
            with ui.row().classes("w-full items-center gap-2"):  # 输入区
                field = ui.input(placeholder="请在这里填写，例如：成都").props("outlined dense").classes("flex-1")  # 输入框
                field.on("keydown.enter", lambda: _choose(view, field.value))  # 回车提交
                ui.button("确定", on_click=lambda: _choose(view, field.value)).props("unelevated dense")  # 确定
        with ui.row().classes("w-full items-center justify-between"):  # 底部
            hint = (  # 引导语按类型
                "填写后点确定或回车。"  # 补参数：填
                if kind == "input"  # 输入型
                else ("请选择确认或拒绝；确认后将执行并留痕。" if kind == "approve" else "点上面的选项继续；也可以直接在下方输入新的描述。")  # 审批/选路由
            )  # 结束
            ui.label(hint).classes("hp-hint")  # 引导
            ui.button("取消本轮", on_click=lambda: _dismiss(view)).props("flat dense").classes("opacity-60")  # 取消（弱化）


def _dismiss(view: dict[str, Any]) -> None:  # 取消澄清
    """清卡并请求取消当前运行（暂停态走专门出口）。"""
    box = view.get("approval_box")  # 容器
    if box is not None and not box.is_deleted:  # 还在
        box.clear()  # 清空
        box.update()  # 刷新
    handle = view.get("handle")  # 句柄
    if handle is not None and hasattr(handle, "cancel"):  # 能取消
        view["run_state"] = "cancelling"  # 立即反馈
        _refresh_header(view)  # 组头
        handle.cancel()  # 请求取消


def _clear_clarify(view: dict[str, Any]) -> None:  # 终态清卡
    """运行结束/失败/取消后清掉澄清卡，避免残留。"""
    box = view.get("approval_box")  # 容器
    if box is not None and not box.is_deleted:  # 还在
        box.clear()  # 清空
        box.update()  # 刷新


def _resume_action(view: dict[str, Any], value: Any) -> None:  # 恢复当前运行
    """清卡→回到运行态→把选择/自由输入交回（兼容同步/异步句柄）。"""
    import asyncio  # 后台任务
    import inspect  # 判断是否协程

    box = view.get("approval_box")  # 容器
    if box is not None and not box.is_deleted:  # 还在
        box.clear()  # 清空
        box.update()  # 刷新
    handle = view.get("handle")  # 句柄（RunSession 同步发 / StreamHandle 异步）
    if handle is None:  # 没有
        return  # 结束
    view["run_state"] = "running"  # 恢复后回到运行态
    _refresh_header(view)  # 组头
    result = handle.resume(value)  # 交回（RunSession 内部起任务，返回 None）
    if inspect.isawaitable(result):  # 异步实现才需要 create_task
        asyncio.create_task(result)  # 起任务


def _choose(view: dict[str, Any], value: str) -> None:  # 选定候选后恢复
    """点澄清卡上的候选 → 恢复同一运行。"""
    _resume_action(view, value)  # 恢复


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


def _elapsed_text(seconds: float) -> str:  # 中文时长（无空格/不补零）
    """把秒数写成 ``4秒`` / ``2分4秒`` / ``1小时2分4秒``（0 的分/秒省略）。"""
    total = int(max(0.0, seconds))  # 取整秒
    if total < 60:  # 一分钟内
        return f"{total}秒"  # 4秒
    minutes, sec = divmod(total, 60)  # 分/秒
    if minutes < 60:  # 一小时内
        return f"{minutes}分{sec}秒" if sec else f"{minutes}分"  # 2分4秒 / 2分
    hours, minutes = divmod(minutes, 60)  # 时/分
    parts = [f"{hours}小时"]  # 时
    if minutes:  # 有分
        parts.append(f"{minutes}分")  # 分
    if sec:  # 有秒
        parts.append(f"{sec}秒")  # 秒
    return "".join(parts)  # 1小时2分4秒


def _running_line(view: dict[str, Any]) -> str:  # 运行中的那行文案
    """``正在思考中，用时X ···``；从发送（含检索阶段）起算。"""
    elapsed = time.perf_counter() - view["started_at"]  # 已用时
    return f"正在思考中，用时{_elapsed_text(elapsed)} ···"  # 文案


def _refresh_header(view: dict[str, Any]) -> None:  # 更新组头
    """运行中显示"正在<当前步> · N 步 · 用时X"，完成显示"已完成 · N 步 · 总耗时"。

    折叠即摘要（方案 A），展开看时间线（方案 B）。
    """
    if view["headline"] is None or not _alive(view):  # 没 UI
        if view.get("ticker") is not None:  # 有计时器也要停（防泄漏）
            with contextlib.suppress(Exception):  # 停表失败不阻断
                view["ticker"].cancel()  # 停
            view["ticker"] = None  # 置空
        return  # 跳过
    steps = view["steps"]  # 有序步骤
    running_step = next((s for s in steps if s.get("status") == "running"), None)  # 当前步
    failed = any(s.get("status") == "failed" for s in steps)  # 失败
    step_n = len(steps)  # 步数
    if view["run_state"] == "error" or failed:  # 失败
        text = "本轮失败"  # 文案
    elif view["run_state"] == "cancelled":  # 停止
        text = "已停止生成"  # 文案
    elif view["run_state"] == "waiting":  # HITL 暂停：等用户
        text = "等待你的选择…"  # 文案
    elif view["run_state"] == "cancelling":  # 正在停止
        text = "正在停止…"  # 文案
    elif running_step is not None:  # 有正在跑的步骤
        running_title = str(running_step.get("title") or "处理")  # 当前步标题
        count = f" · {step_n} 步" if step_n else ""  # 步数
        text = f"正在{running_title}{count} · {_elapsed_text(time.perf_counter() - view['started_at'])}"  # 实时文案
    elif view.get("run_state") in ("context_loading", "retrieving"):  # 准备/检索
        text = _running_line(view)  # 努力求索中，用时X ···
    elif view["run_state"] in ("done",):  # 完成
        text = f"已完成 · {step_n} 步" if step_n else "已完成"  # 摘要
    else:  # 初始
        text = _running_line(view)  # 努力求索中，用时X ···
    terminal = view["run_state"] in ("done", "error", "cancelled")  # 是否到终态
    waiting = view["run_state"] == "waiting"  # 是否等用户
    view["headline"].style(  # 运行中"文案+时间"用强调色
        f"color: {'var(--hp-accent)' if (not terminal and not waiting) else 'inherit'}"
    )  # 样式结束
    if (terminal or waiting) and view.get("ticker") is not None:  # 到终态/暂停：停表
        with contextlib.suppress(Exception):  # 停表失败不阻断
            view["ticker"].cancel()  # 停止
        view["ticker"] = None  # 置空
    view["headline"].text = text  # 写
    view["meta"].text = _meta_text(view)  # 右侧
    view["headline"].update()  # 刷新
    view["meta"].update()  # 刷新
    alive = view["run_state"] not in ("done", "error", "cancelled", "waiting")  # 暂停时不再转圈
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
