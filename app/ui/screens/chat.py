"""第一个功能页面：聊天。

**为什么叫"聊天"而不是"问答"**：这不是一个只用来问一句答一句的系统——
会话、过程、引用、以后的工具调用都会长在这里，"聊天"才是它真实的范围。

**结构借鉴 DSH 的 Chat**：

- 一轮 = **用户气泡 + 过程行（可收起）+ 答案（永不收起）**；
- 间距照它的规格：过程行之间 6px、答案与过程行 12px、引用行与答案 8px；
- 过程行文字默认 tertiary、悬停 secondary；运行中有一道从左到右的高光扫过；
- 出处是**可以 Tab 到的按钮**，点开在右栏看片段全文（DSH 同样是"引用点开侧栏"）；
- 报错用语义色，和"没找到"不能长一样。

**多会话**：内容属于"当前会话"（`app/state/conversations.py`）。切会话时这里整块重画；
发消息时**只增量画这一轮**——过程行有动画，整块重画会把动画抹掉。

**数据源现在是模拟的**（用户要求：先只做界面）——见 `app/screens/_chat_mock.py`。
接真实数据时只要把 `pick_turn` 换成"检索结果转成 `Turn`"，这个文件不用改。
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from nicegui import ui

from app.ui.screens import _chat_mock
from app.ui.state import conversations
from app.ui.state.chat_model import Entry, Fragment, Turn
from app.ui.state.layout import layout


def build() -> None:
    """搭出聊天页（由外壳在切到本页时调用一次）。"""
    with ui.column().classes('w-full h-full gap-0'):
        scroll = ui.scroll_area().classes('w-full flex-1 min-h-0')
        with scroll:
            stream = ui.column().classes('w-full hp-measure gap-8 px-4 py-8')
        _build_composer(stream, scroll)

    def render_current() -> None:
        """把当前会话整块重画（切会话时用；历史不需要动画）。"""
        stream.clear()
        conversation = conversations.current()
        with stream:
            if conversation.note:
                _render_notice(conversation.note)
            if not conversation.entries:
                _render_empty_hint()
        for entry in conversation.entries:
            _render_history_turn(stream, entry)

    conversations.subscribe_select(render_current)
    _chat_mock.ensure_seeded()
    render_current()


def _render_notice(note: str) -> None:
    """会话级说明（示例会话用它标注"这是模拟数据"）。"""
    with ui.row().classes('w-full items-start gap-2 no-wrap'):
        ui.icon('science').classes('text-base hp-hint')
        ui.label(note).classes('hp-hint')


def _render_empty_hint() -> None:
    """空会话的引导（"新对话"点进来就是这一屏）。"""
    with ui.column().classes('w-full gap-1'):
        ui.label('这个对话还是空的').classes('hp-page-title')
        ui.label('在下面问点什么就行。Enter 发送，Shift+Enter 换行。').classes('hp-hint')


def _build_composer(stream: Any, scroll: Any) -> None:
    """底部输入区（固定在窗口底部，不跟着对话流滚）。"""

    async def send() -> None:
        text = (box.value or '').strip()
        if not text:
            return
        box.value = ''
        conversation = conversations.current()
        if not conversation.entries:
            stream.clear()  # 第一条消息进来时去掉"这个对话还是空的"或示例标注
        turn = _chat_mock.pick_turn(text)
        # 先增量画这一轮（有"运行中"动画）……
        await _append_live_turn(stream, turn, question=text)
        # ……再落进会话数据；`append` 只通知左栏列表重画，不动对话流
        conversations.append(Entry(question=text, turn=turn))
        box.run_method('focus')  # 连续追问不用每次用鼠标点一下输入框
        scroll.scroll_to(percent=1.0)

    with (
        ui.column().classes('w-full hp-composer'),
        ui.column().classes('w-full hp-measure px-4 py-3'),
        ui.row().classes('w-full items-end gap-1 hp-composer-box'),
    ):
        box = (
            ui.textarea(placeholder='问点什么…（Enter 发送，Shift+Enter 换行）')
            .props('autogrow borderless dense autofocus')
            .classes('flex-1')
            .mark('chat-input')  # 界面测试用锚点（见 tests/ui/）
        )
        ui.button(icon='send', on_click=send).props('round unelevated').classes(
            'hp-send'
        ).mark('chat-send')

    # Enter 发送：`.exact` 让 Shift+Enter 保持换行
    box.on('keydown.enter.exact', send)


def _render_turn_shell(stream: Any, turn: Turn, question: str | None) -> tuple[Any, Any]:
    """建出一轮的骨架（用户气泡 + 过程行），返回 ``(容器, 过程行)``。"""
    with stream:
        if question:
            ui.label(question).classes('hp-bubble-user')
        box = ui.column().classes('w-full hp-turn')
        process_row = None
        if turn.process is not None:
            with box:
                process_row = _build_process_row()
    return box, process_row


def _render_turn_body(box: Any, turn: Turn) -> None:
    """填这一轮的正文：答案 + 引用，或者一句说明。"""
    with box:
        if turn.answer:
            ui.markdown(turn.answer).classes('hp-markdown w-full')
            _render_citations(turn.fragments)
        elif turn.note:
            ui.label(turn.note).classes(
                'hp-note-error' if turn.note_kind == 'error' else 'hp-hint'
            )


def _render_history_turn(stream: Any, entry: Entry) -> None:
    """画一轮**历史**（切会话时重画）：过程行直接落到结果，不放动画。"""
    box, process_row = _render_turn_shell(stream, entry.turn, entry.question)
    if entry.turn.process is not None and process_row is not None:
        _fill_process_row(process_row, entry.turn)
    _render_turn_body(box, entry.turn)


async def _append_live_turn(stream: Any, turn: Turn, *, question: str) -> None:
    """画一轮**刚发生的**：过程行先显示"运行中"（带微光），停一帧再落到结果。"""
    box, process_row = _render_turn_shell(stream, turn, question)
    if turn.process is not None and process_row is not None:
        _row, title, _details = process_row
        title.text = turn.process.running
        title.classes(add='hp-shimmer')
        await asyncio.sleep(0.12)  # 让"运行中"这一帧真的画出来（本地很快，但仍要给反馈）
        title.classes(remove='hp-shimmer')
        _fill_process_row(process_row, turn)
    _render_turn_body(box, turn)


def _build_process_row() -> tuple[Any, Any, Any]:
    """建一个过程行，返回 ``(行, 标题, 详情容器)``。

    **默认收起**（DSH：先看答案，想看过程再展开）。
    """
    row = ui.expansion().classes('w-full hp-process')
    with (
        row.add_slot('header'),
        ui.row().classes('w-full items-center gap-2 no-wrap hp-process-row'),
    ):
        ui.icon('travel_explore').classes('text-base')
        title = ui.label('').classes('hp-process-title')
    with row:
        details = ui.column().classes('w-full gap-0 pt-1 pl-6')
    return row, title, details


def _fill_process_row(process_row: tuple[Any, Any, Any], turn: Turn) -> None:
    """把过程行落到"已完成"并填上可展开的细节。"""
    _row, title, details = process_row
    assert turn.process is not None  # 调用方已判空；这里只是给类型检查器看
    title.text = turn.process.done
    title.update()
    with details:
        for line in turn.process.details:
            ui.label(line).classes('hp-process-detail')


def _render_citations(fragments: list[Fragment]) -> None:
    """引用角标一行：按钮，所以能 Tab 到、回车能开。"""
    if not fragments:
        return
    with ui.row().classes('w-full items-center gap-1 flex-wrap pt-1'):
        ui.label('出处').classes('hp-hint pr-1')
        for fragment in fragments:
            ui.button(str(fragment.index), on_click=partial(_open_fragment, fragment)).props(
                'flat dense no-caps'
            ).classes('hp-cite').mark(f'cite-{fragment.index}')
            ui.label(f'{fragment.source_name} · {fragment.origin}').classes('hp-process-detail pr-2')


def _open_fragment(fragment: Fragment) -> None:
    """点引用角标 → 片段全文进右栏（DSH 的引用也是点开侧栏）。"""
    layout().show_right(partial(_render_fragment_panel, fragment))


def _render_fragment_panel(fragment: Fragment) -> None:
    """右栏内容：出处信息 + 片段全文（按 Markdown 渲染）。"""
    with ui.column().classes('w-full gap-3 px-4 py-4').mark('fragment-panel'):
        ui.label(fragment.source_name).classes('text-sm font-medium')
        ui.label(f'{fragment.origin} · bm25 {fragment.score:.2f}').classes('hp-hint')
        ui.separator()
        ui.markdown(fragment.text).classes('hp-markdown w-full')
