"""聊天页的历史轮渲染（过程组、步骤、思考、答案预览）。"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from app.services import settings as settings_service
from app.ui.screens.chat.citations import _render_citations
from app.ui.screens.chat.dialogs import _open_settings_dialog
from app.ui.screens.chat.state import _STEP_ICONS
from app.ui.screens.chat.widgets import (
    _render_answer_text,
    _render_attachments,
    _render_basis_content,
    _render_steps_flow,  # 时间线（与实时展开共用）
)
from app.ui.state import presentation
from app.ui.state.chat_model import Entry, Step, Turn


def _settle_turn(box: Any, turn: Turn) -> None:
    """检索完成后，把"等待中"那一轮换成正式的一轮（折起的过程组 + 答案）。"""
    # 路径：清掉等待行（timer 一起取消），再画正式内容（用户气泡不在 box 里，保留）。
    box.clear()  # 删掉等待行（秒表 timer 随元素一起取消）
    with box:
        # 路径 1：有过程组 → 先画可折叠过程；路径 2：没有就只画正文。
        if turn.process is not None:
            _render_process(turn)
        _render_turn_body(box, turn)


def _render_turn_body(box: Any, turn: Turn, *, history: bool = False) -> None:
    """填这一轮的正文：答案 + 引用，或者一句说明。"""
    with box:
        # 路径 1：有答案 → Markdown 正文 + 引用区（默认收起）。
        if turn.answer:
            _render_answer_text(turn.answer, history=history)  # 历史长答案折叠
            _render_citations(turn.fragments)
        # 路径 2：没有答案但有说明 → 按 note_kind 选择错误色或弱提示色。
        elif turn.note:
            ui.label(turn.note).classes(
                'hp-note-error' if turn.note_kind == 'error' else 'hp-hint'
            )
        if turn.conflict:  # 资料冲突
            ui.label(f"⚠️ 资料与通用知识可能冲突：{turn.conflict}").classes("hp-note-warning")


def _render_history_turn(stream: Any, entry: Entry) -> Any:
    """画一轮**历史**（切会话时重画）：过程组折起，不放动画。"""
    # 路径：与实时轮相同的结构，但没有等待行/定时器（历史不需要动画）。
    with stream:
        ui.label(entry.question).classes('hp-bubble-user')
        _render_attachments(entry.attachments)
        box = ui.column().classes('w-full hp-turn')
    with box:
        if entry.turn.process is not None:
            _render_process(entry.turn)
        _render_history_extras(entry.turn)
        _render_turn_body(box, entry.turn, history=True)  # 历史：长答案默认折叠
    return box  # 供轮次导航定位


def _render_history_extras(turn: Turn) -> None:  # 历史里的依据/设置
    """检索依据 + 本轮设置按钮。"""
    if turn.basis:  # 有依据
        counts = turn.basis.get("hit_counts") or {}  # 计数
        with ui.expansion(f"检索依据 · 命中 {counts.get('selected', 0)} 段资料").classes(
            "w-full hp-process"
        ):
            _render_basis_content(turn.basis)
    if turn.settings and settings_service.get_log_debug():  # 本轮设置是调试信息：只在调试模式展示
        basis = turn.basis or {}  # 检索依据（改写/查询/门限都在里面）
        usage_data = turn.usage or {}  # 用量
        ui.button("本轮设置", on_click=lambda: _open_settings_dialog(  # 设置按钮
            turn.settings,  # 基础设置
            rewrite=basis.get("rewrite"),  # 改写结论
            queries=basis.get("queries"),  # 实际查询
            gate=basis.get("gate"),  # 门限结论
            usage=usage_data,  # token 用量
            duration_ms=usage_data.get("total_ms"),  # 总耗时
            ttft_ms=usage_data.get("first_token_ms"),  # 首字耗时
        )).props("flat dense no-caps").classes("hp-field-help")  # 弱化样式
    if turn.usage:  # 有用量
        parts: list[str] = []  # 片段
        if turn.fragments:  # 引用
            parts.append(f"{len(turn.fragments)} 段引用")  # 引用数
        if turn.answer:  # 字数
            parts.append(f"{len(turn.answer)} 字")  # 字数
        if turn.usage.get("total_ms"):  # 耗时
            parts.append(f"{int(turn.usage['total_ms']) / 1000:.1f}s")  # 秒
        if parts:  # 有内容
            ui.label(" · ".join(parts)).classes("hp-field-help")  # 用量


def _render_process(turn: Turn) -> None:
    """渲染过程组（DSH 的 process group，固定「标准」档）。

    完成的一轮折起，点开才看到推理预览与步骤明细。
    标题 = 步骤名（最多 3 个）+ 总耗时，见 :func:`presentation.process_title`。
    """
    process = turn.process
    assert process is not None
    title = process.done if process.done.startswith(("已", "本轮", "没有")) else presentation.process_title(process)
    if turn.usage.get("total_ms"):  # 有总耗时
        title = f"{title} · {int(turn.usage['total_ms']) / 1000:.1f}s"  # 追加

    # 路径：外层折叠面板；header 放图标 + 标题，正文放推理与步骤明细。
    row = ui.expansion().classes('w-full hp-process')
    with (
        row.add_slot('header'),
        ui.row().classes('w-full items-center gap-2 no-wrap hp-process-row'),
    ):
        ui.label(title).classes('hp-process-title')
    with row:
        _render_process_body(process)


def _render_process_body(process: Any) -> None:
    """过程组正文：推理 + 有序时间线（与实时展开**同一渲染器**）。"""
    with ui.column().classes('w-full gap-0 pt-1 pl-6'):
        # 路径 1：有推理预览才画"推理"折叠块。
        if process.reasoning:
            _render_reasoning(process.reasoning)
        # 路径 2：时间线（有序步骤；空列表就不画）。实时/历史用同一份渲染。
        _render_steps_flow(process.steps)


def _render_reasoning(text: str) -> None:
    """推理预览：本地检索口径的"理解与依据"，可展开看全文。

    **不冒充模型思维链**：本应用还没接生成式 LLM，块里写的是真实的检索参数与命中；
    接上以后换成模型流式的 reasoning，这个渲染器不用改。
    """
    # 路径：取第一行非空文本当预览；去掉 Markdown 列表前缀 "- " 再截断。
    preview = next((line.strip() for line in text.splitlines() if line.strip()), '')
    if preview.startswith('- '):
        preview = preview[2:]
    row = ui.element('details').classes('w-full hp-reasoning')
    with row:
        with ui.element('summary').classes('w-full items-center gap-2 no-wrap hp-reasoning-row'):
            ui.icon('psychology').classes('text-sm hp-reasoning-icon')
            ui.label('思考过程').classes('hp-reasoning-title')
            ui.label(preview[:48]).classes('hp-hint hp-reasoning-preview')
        # 展开后看完整思考（Markdown）。
        ui.markdown(text).classes('hp-md-compact')


def _render_step(step: Step) -> None:
    """一步 = 可展开的一行（对应 DSH 的工具/命令行）：图标 + 标题 + 明细。"""
    row = ui.expansion().classes('w-full hp-step')
    with (
        row.add_slot('header'),
        ui.row().classes('w-full items-center gap-2 no-wrap hp-step-row'),
    ):
        ui.icon(_STEP_ICONS.get(step.group, 'chevron_right')).classes('text-sm hp-step-icon')
        ui.label(step.title).classes('hp-step-title')
        ui.space()
        # 路径：失败步骤追加错误图标；成功步骤不加。
        if step.status == 'failed':
            ui.icon('error').classes('text-sm hp-step-error')
    with row:
        # 路径：有明细显示明细；没有就显示"（无明细）"，避免展开是空白。
        ui.label(step.detail or '（无明细）').classes('hp-step-detail-text')
