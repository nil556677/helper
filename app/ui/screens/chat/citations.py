"""聊天页的引用渲染与片段面板。"""

from __future__ import annotations

from functools import partial
from typing import Any

from nicegui import ui

from app.services import chat_stats
from app.ui.screens.chat.state import _TIER_LABELS
from app.ui.state.chat_model import Fragment
from app.ui.state.layout import layout


def _render_block_detail(container: Any, payload: dict[str, Any]) -> None:  # 渲染分块明细
    """把一轮的 `context_usage` 展开成大板块 / 小板块 / 最细一层。"""
    container.clear()  # 清空旧内容
    with container:  # 往容器里画
        if not payload:  # 没有数据（老历史）
            ui.label("本轮没有上下文占用数据（该轮早于分块统计）").classes("hp-hint")  # 提示
            return  # 结束
        window = int(payload.get("window") or 0) or chat_stats.CONTEXT_LIMIT  # 窗口
        total = int(payload.get("total") or 0)  # 总量
        ui.label(f"本轮 {chat_stats.format_tokens(total)} / {chat_stats.format_tokens(window)}").classes("hp-hint")  # 总览
        tiers = payload.get("tiers") or {}  # 大板块
        for key, label in _TIER_LABELS.items():  # 三个大板块
            size = int(tiers.get(key) or 0)  # 大小
            pct = (size / window * 100) if window else 0.0  # 占比
            ui.label(f"{label}：{chat_stats.format_tokens(size)}（{pct:.1f}%）").classes("hp-hint")  # 一行
        blocks = payload.get("blocks") or {}  # 小板块
        if blocks:  # 有就合并成一行
            ui.label(" · ".join(f"{key} {chat_stats.format_tokens(int(value))}" for key, value in blocks.items())).classes("hp-hint")  # 小板块行
        for key, items in (payload.get("subblocks") or {}).items():  # 最细一层
            if isinstance(items, dict) and items:  # 有效
                detail = " · ".join(f"{name} {chat_stats.format_tokens(int(value))}" for name, value in items.items())  # 拼接
                ui.label(f"{key} 明细：{detail}").classes("hp-hint")  # 一行
        badges: list[str] = []  # 压缩/裁剪徽标
        if payload.get("compacted"):  # 压缩过
            badges.append(f"已压缩 {int(payload['compacted'])} 次")  # 记
        if payload.get("trimmed"):  # 裁剪过
            badges.append(f"裁剪 {int(payload['trimmed'])} 条")  # 记
        if badges:  # 有徽标
            ui.label(" · ".join(badges)).classes("hp-hint")  # 一行


def _render_citations(fragments: list[Fragment]) -> None:
    """资料区：**默认收起**的一行，点开才列出每条出处（按钮，点开右栏看全文）。

    不再把"出处 + 每条来源"整行直接铺在答案下面——长答案会拖出一条很长的
    来源清单。DSH 的引用同样是"先给一个可点的入口，要看再进侧栏"。
    """
    # 路径 1：没有引用 → 不画这一行（例如无命中/失败轮）。
    if not fragments:
        return
    # 路径 2：有引用 → 折叠行，header 显示"资料 · N 段"，展开后逐条列。
    row = ui.expansion().classes('w-full hp-sources-row')
    with (
        row.add_slot('header'),
        ui.row().classes('w-full items-center gap-2 no-wrap hp-sources-head'),
    ):
        ui.icon('attach_file').classes('text-base')
        ui.label(f'资料 · {len(fragments)} 段').classes('hp-sources-title')
    with row, ui.column().classes('w-full gap-0 pt-1 pl-6'):
        for fragment in fragments:
            # 路径：每条 = 角标按钮 + 来源名；两者都能点开右栏看全文。
            with ui.row().classes('w-full items-center gap-2 no-wrap hp-source-line'):
                ui.button(
                    str(fragment.index), on_click=partial(_open_fragment, fragment)
                ).props('flat dense no-caps').classes('hp-cite').mark(
                    f'cite-{fragment.index}'
                ).tooltip('点开看原文')
                ui.label(f'{fragment.source_name} · {fragment.origin}').classes(
                    'hp-source-name'
                ).on('click', partial(_open_fragment, fragment)).tooltip('点开看原文')


def _open_fragment(fragment: Fragment) -> None:
    """点引用角标 → 片段全文进右栏（DSH 的引用也是点开侧栏）。"""
    # 路径：把"画右栏内容"的回调交给外壳，由 layout 负责清空/头行/展开。
    layout().show_right(partial(_render_fragment_panel, fragment))


def _render_fragment_panel(fragment: Fragment) -> None:
    """右栏内容：**具体位置** + 命中片段 + 所在资料全文。

    "位置：第 N 段 / 第 N 页"让用户知道这段原文到底在哪；命中片段高亮出来，
    下面再给整段证据正文，避免只看到一大段文字却不知道是哪一处命中的。
    """
    with ui.column().classes('w-full gap-3 px-4 py-4').mark('fragment-panel'):
        ui.label(fragment.source_name).classes('text-sm font-medium')
        ui.label(f'位置：{fragment.origin}').classes('hp-hint')
        ui.label(f'{fragment.score_kind} {fragment.score:.2f}').classes('hp-hint')
        # 路径：只有"命中片段"与"证据正文"确实不同才单独展示，避免重复一大段。
        if fragment.snippet and fragment.snippet.strip() != fragment.text.strip():
            ui.label('命中片段').classes('hp-hint pt-1')
            ui.label(fragment.snippet).classes('hp-snippet')
        ui.separator()
        ui.label('所在资料全文').classes('hp-hint')
        ui.markdown(fragment.text).classes('hp-markdown w-full')
