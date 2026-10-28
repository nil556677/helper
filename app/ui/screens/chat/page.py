"""聊天页的页面骨架（布局、轮次导航、历史重画、加载更早）。"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from app.ui.screens.chat.composer import _build_composer
from app.ui.screens.chat.history import _render_history_turn
from app.ui.screens.chat.scroll import _SCROLL_JS, _scroll_to_box, _set_follow
from app.ui.screens.chat.state import _PAGE_TURNS, _detach_live_views, _LiveState, _sync_composer
from app.ui.state import conversations


def build() -> None:
    """搭出聊天页（由外壳在切到本页时调用一次）。"""
    live_state = _LiveState()  # 本页的实时状态（每开一个窗口一份）
    turn_refs: list[Any] = []  # 每轮容器，供导航定位
    nav = {'index': -1}  # 当前轮索引
    window = {"limit": _PAGE_TURNS}  # 历史窗口：只画最近这么多轮

    tick_els: list[Any] = []  # 右侧轮次轨道的刻度（与 turn_refs 同序）

    def _update_nav() -> None:  # 更新轮次导航
        """共 N 轮 + 当前第几轮。"""
        total = len(turn_refs)  # 总数
        if not total:  # 空会话
            nav_label.text = "暂无轮次"  # 文案
            prev_btn.set_enabled(False)  # 禁用
            next_btn.set_enabled(False)  # 禁用
            return  # 结束
        nav['index'] = max(0, min(nav['index'], total - 1))  # 夹住
        nav_label.text = f"第 {nav['index'] + 1} / {total} 轮"  # 文案
        prev_btn.set_enabled(nav['index'] > 0)  # 上一轮
        next_btn.set_enabled(nav['index'] < total - 1)  # 下一轮

    def _set_active(position: int) -> None:  # 高亮某一轮
        """同时更新导航文案、右侧"第几轮"与轨道刻度。"""
        nav['index'] = position  # 记下当前
        total = len(turn_refs)  # 总轮数
        rail_label.text = f"{position + 1}/{total}" if total and position >= 0 else "—"  # 右侧数字
        rail_label.update()  # 刷新
        for index, tick in enumerate(tick_els):  # 逐刻度
            if tick is None or tick.is_deleted:  # 已重建
                continue  # 跳过
            if index == position:  # 当前
                tick.classes(add="hp-turn-tick--active")  # 加高亮
            else:  # 其他
                tick.classes(remove="hp-turn-tick--active")  # 去掉高亮
            tick.update()  # 刷新
        _update_nav()  # 刷新文案

    def _jump_to(position: int) -> None:  # 跳到某一轮
        """点刻度/按导航都走这里：滚过去并高亮。"""
        if not (0 <= position < len(turn_refs)):  # 越界
            return  # 跳过
        _set_active(position)  # 高亮
        box = turn_refs[position]  # 容器
        if box is not None and not box.is_deleted:  # 还在
            box.run_method('scrollIntoView', {'behavior': 'smooth', 'block': 'start'})  # 滚过去

    def _scroll_turn(delta: int) -> None:  # 上/下一轮
        """按相对位置跳。"""
        if not turn_refs:  # 没有轮
            return  # 跳过
        _jump_to(max(0, min(nav['index'] + delta, len(turn_refs) - 1)))  # 跳

    def render_current() -> None:
        """把当前会话整块重画；只画最近 `window['limit']` 轮，更早的点按钮往前翻。"""
        # 路径：先摘掉正在跑的实时视图，避免它们继续更新已删除的元素。
        _detach_live_views(live_state)
        stream.clear()
        turn_refs.clear()  # 清空轮次引用
        conversation = conversations.current()
        entries = list(conversation.entries)  # 全部轮次
        shown = entries[-max(1, int(window["limit"])):]  # 只画窗口内的
        hidden = len(entries) - len(shown)  # 窗口外还剩多少轮
        with stream:
            # 路径 0：有更早的轮次 → 顶部放"加载更早"。
            if hidden > 0:
                ui.button(
                    f"加载更早的 {min(_PAGE_TURNS, hidden)} 轮（共 {len(entries)} 轮）",
                    on_click=_load_more,
                ).props("flat dense no-caps").classes("hp-field-help").mark("chat-load-more")
            # 路径 1：会话带说明（如"这是示例数据"）→ 顶部画一条提示。
            if conversation.note:
                _render_notice(conversation.note)
            # 路径 2：空会话 → 画引导语（"这个对话还是空的"）。
            if not entries:
                _render_empty_hint()
        # 路径 3：逐轮重画历史（每轮 = 用户气泡 + 过程组 + 答案/引用）。
        for entry in shown:
            turn_refs.append(_render_history_turn(stream, entry))
        _rebuild_rail()  # 重建右侧刻度
        nav['index'] = len(turn_refs) - 1  # 默认最后一轮
        # 路径 4：这个会话还有后台任务在跑 → 至少给一条“正在处理”的提示。
        active_run = live_state.active.get(conversation.id)
        if active_run:
            with stream:
                _render_active_hint(str(active_run.get("question") or ""))
        # 重画后底栏统计要跟着当前会话刷新。
        update_stats()
        _sync_composer(live_state)  # 同步发送/停止按钮
        _set_active(len(turn_refs) - 1)  # 高亮最后一轮（空会话会走 _update_nav 的兜底）
        _set_follow(True)  # 重画后落在最后一行

    def _rebuild_rail() -> None:  # 重建轮次轨道
        """一条刻度 = 一轮；点刻度跳到那一轮。"""
        rail_ticks.clear()  # 只清刻度（顶部"第几轮"要留着）
        tick_els.clear()  # 清引用
        with rail_ticks:  # 重建
            for position in range(len(turn_refs)):  # 每一轮
                tick = ui.element('div').classes('hp-turn-tick')  # 刻度
                tick.on('click', lambda _e=None, pos=position: _jump_to(pos))  # 点击跳转
                tick_els.append(tick)  # 记下

    def _load_more() -> None:
        """往前多画一页；把视口锚在原来第一条上，别让读者被弹到顶部。"""
        total = len(conversations.current().entries)  # 总轮数
        old_first = max(0, total - int(window["limit"]))  # 原来第一条可见的下标
        window["limit"] = int(window["limit"]) + _PAGE_TURNS  # 扩大窗口
        render_current()  # 重画
        new_first = max(0, total - int(window["limit"]))  # 现在第一条可见的下标
        target = old_first - new_first  # 原来那条在新列表里的位置
        if 0 <= target < len(turn_refs):  # 还在窗口里
            _scroll_to_box(turn_refs[target])  # 滚回它（保持阅读位置）
        _set_follow(False)  # 往上翻时别自动跟到底（否则刚锚好又被拉走）

    # 布局：整页一列（占满高度）；里面是导航 + 可滚动区域 + 底部输入区。
    with ui.column().classes('w-full h-full gap-0'):
        with ui.row().classes('w-full items-center gap-2 px-4 pt-2 no-wrap'):
            ui.space()  # 靠右
            nav_label = ui.label("暂无轮次").classes("hp-field-help")  # 轮次
            prev_btn = ui.button(icon='keyboard_arrow_up', on_click=lambda: _scroll_turn(-1)).props(
                'flat dense round size=sm'
            ).tooltip('上一轮')  # 上一轮
            next_btn = ui.button(icon='keyboard_arrow_down', on_click=lambda: _scroll_turn(1)).props(
                'flat dense round size=sm'
            ).tooltip('下一轮')  # 下一轮
        with ui.row().classes('w-full flex-1 min-h-0 no-wrap gap-0 items-stretch'):
            with ui.column().classes('flex-1 min-w-0 min-h-0'):
                scroll = ui.scroll_area().classes('w-full h-full hp-chat-scroll')
                with scroll:
                    stream = ui.column().classes('w-full hp-measure gap-8 px-4 py-8')
            rail_box = ui.element('div').classes('hp-turn-rail')  # 右侧轮次轨道
            with rail_box:  # 轨道内容：顶部"第几轮"，下面一列刻度
                rail_label = ui.label("").classes('hp-turn-rail-label')  # 第几轮
                rail_ticks = ui.element('div').classes('hp-turn-ticks')  # 刻度容器
        # 输入区在滚动区外（固定在底部），并返回统计刷新函数。
        update_stats = _build_composer(stream, scroll, live_state, on_refresh=render_current)

    # 浏览器侧"贴底跟随"（学 DSH 的滚动归属）：只在读者处于底部时自动跟随。
    ui.add_body_html(_SCROLL_JS)  # 每个页面注入一次

    # 路径：切会话时重画对话流；第一次进来先保证至少有一个会话，再画。
    conversations.subscribe_select(render_current)
    conversations.ensure_seeded()
    render_current()


def _render_active_hint(question: str) -> None:  # 正在处理的提示
    """切回一个仍在跑的会话时，至少让用户知道它在跑。"""
    with ui.row().classes('w-full items-center gap-2 no-wrap hp-running-row'):
        ui.icon('travel_explore').classes('text-base hp-running-icon')
        ui.label(f'正在处理：{question[:60]}…').classes('hp-running-text hp-shimmer-accent')


def _render_notice(note: str) -> None:
    """会话级说明（示例会话用它标注"这是模拟数据"）。"""
    # 路径：一行图标 + 弱提示文字。
    with ui.row().classes('w-full items-start gap-2 no-wrap'):
        ui.icon('science').classes('text-base hp-hint')
        ui.label(note).classes('hp-hint')


def _render_empty_hint() -> None:
    """空会话的引导（"新对话"点进来就是这一屏）。"""
    # 路径：标题 + 操作提示（Enter 发送 / Shift+Enter 换行）。
    with ui.column().classes('w-full gap-1'):
        ui.label('这个对话还是空的').classes('hp-page-title')
        ui.label('在下面问点什么就行。Enter 发送，Shift+Enter 换行。').classes('hp-hint')
