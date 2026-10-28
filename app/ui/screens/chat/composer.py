"""聊天页的输入区（附件、队列、发送/停止、事件消费）。"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from loguru import logger
from nicegui import events, ui

from app.services import attachments as attachment_service
from app.services import chat_stats
from app.ui.components.dialog import notify
from app.ui.screens.chat import streaming
from app.ui.screens.chat.citations import _render_block_detail
from app.ui.screens.chat.state import _ActiveStream, _LiveState
from app.ui.screens.chat.widgets import _seg_width
from app.ui.state import conversations
from app.ui.state.chat_queue import MessageQueue, QueuedMessage


def _build_composer(stream: Any, scroll: Any, live_state: _LiveState, *, on_refresh: Any = None) -> Any:
    """底部输入区 + 统计 dock（固定在窗口底部，不跟着对话流滚）。

    on_refresh：后台任务在“非当前会话/视图已被摘掉”时完成，需要重画当前会话。
    live_state：本页的实时状态（视图列表 / 在跑任务 / 按钮同步器）。
    返回 ``refresh_stats()``：整块重画后由外层调用一次，把底栏统计刷成当前会话。
    """

    busy = {'value': False}  # 是否已有检索在跑：只决定“立即跑还是排队”，不拦输入
    queue = MessageQueue()  # 待发送队列（UI-free，自带上限检查，见 chat_queue.py）
    queue_box = None  # 队列展示区（输入框上方；建 UI 时赋值）
    active = _ActiveStream()  # 当前流式句柄 + 它属于哪个会话
    send_button_holder: dict[str, Any] = {}  # 延迟拿到按钮元素
    pending_files: list[dict[str, Any]] = []  # 待发送附件
    attach_box: Any = None  # 附件 chip 容器

    def _render_pending_files() -> None:  # 画待发送附件
        """输入框上方的 chip；每项可移除。"""
        if attach_box is None or attach_box.is_deleted:  # 页面已重建
            return
        attach_box.clear()
        with attach_box:
            if not pending_files:  # 空
                return
            with ui.row().classes("w-full items-center gap-2 flex-wrap pt-1"):
                for item in pending_files:
                    with ui.row().classes("items-center gap-1 hp-attachment-chip"):
                        path = Path(str(item.get("path") or ""))
                        if item.get("kind") == "image" and path.is_file():  # 图片
                            ui.image(str(path)).classes("hp-attach-thumb-sm")  # 缩略图
                        else:  # 文件
                            ui.icon("attach_file").classes("text-sm hp-hint")  # 图标
                        name = str(item.get("name") or "附件")
                        ui.label(name).classes("hp-field-help hp-attach-name").tooltip(name)  # 名字
                        size = int(item.get("size") or 0)
                        if size:  # 大小
                            ui.label(f"{size / 1024:.0f}KB").classes("hp-hint")  # 大小
                        ui.button(icon="close", on_click=partial(_drop_file, item)).props(
                            "flat dense round size=sm"
                        ).tooltip("移除附件")

    def _drop_file(item: dict[str, Any]) -> None:  # 移除附件
        """从待发送列表删掉并删盘。"""
        if item in pending_files:  # 在列表
            pending_files.remove(item)  # 删
            attachment_service.remove(item)  # 删盘
            _render_pending_files()  # 重画

    async def _on_upload(event: events.UploadEventArguments) -> None:  # 上传附件
        """流式落盘；只保留元数据与可读文本。"""
        name = str(event.file.name or "附件")  # 名字
        target = None  # 目标
        try:  # 失败清理
            target, safe = attachment_service.prepare(name)  # 目标
            await event.file.save(target)  # 落盘
            if target.stat().st_size > attachment_service.MAX_BYTES:  # 超限
                raise ValueError(f"单个附件不能超过 {attachment_service.MAX_BYTES // 1024 // 1024}MB")  # 拒绝
            pending_files.append(attachment_service.finalize(target, safe))  # 记录
            _render_pending_files()  # 重画
        except Exception as exc:  # 失败
            logger.opt(exception=exc).warning("附件上传失败：{}", name)  # 日志
            if target is not None:  # 有半成品
                attachment_service.remove({"path": str(target)})  # 删
            notify(f"附件「{name}」上传失败：{exc}", kind="negative")  # 提示

    def _set_running(running: bool) -> None:  # 切换发送/停止
        """生成中显示停止图标；结束后恢复发送。"""
        button = send_button_holder.get('button')  # 按钮
        if button is None or button.is_deleted:  # 还没建/页面已重建
            return  # 跳过
        button.props(f'icon={"stop" if running else "send"}')  # 换图标
        button.props(remove='loading')  # 清掉旧 loading
        button.set_enabled(True)  # 可点
        button.update()  # 刷新

    def _sync_button() -> None:  # 按当前会话同步按钮
        """只在“当前会话正在跑”时显示停止，避免 A 跑着 B 的按钮变成停止。"""
        running = active.belongs_to(conversations.current().id)  # 当前会话是否有活动流
        _set_running(running)  # 更新按钮
        render_queue()  # 队列也只显示当前会话

    async def send() -> None:  # 发送（委托给 streaming，refs 在函数末尾建好）
        await streaming.send(refs)

    async def _on_send_click() -> None:  # 按钮统一入口（有流就停，否则发送）
        await streaming.on_send_click(refs)

    live_state.sync_composer = _sync_button  # 切会话时同步按钮状态

    def render_queue() -> None:
        """把“还没开始执行”的队列画到输入框上方；只显示当前会话。"""
        # 路径：页面已重建（旧 composer 的队列区被删）→ 不再操作旧 DOM。
        if queue_box is None or queue_box.is_deleted:
            return
        current_id = conversations.current().id  # 当前会话
        items = [item for item in queue.items() if item.conversation_id == current_id]  # 过滤
        queue_box.clear()
        with queue_box:
            # 路径 1：当前会话没有排队 → 什么都不画。
            if not items:
                return
            # 路径 2：有排队 → 标题 + 每条的文本/编辑/删除按钮。
            chars = sum(len(item.text) for item in items)  # 当前会话字符数
            with ui.row().classes('w-full items-center gap-2'):
                ui.icon('schedule').classes('text-sm hp-hint')
                ui.label(
                    f'待发送 {len(items)}/{queue.max_items} 条 · 共 {chars}/{queue.max_chars} 字'
                    '（未执行前可编辑/删除）'
                ).classes('hp-hint')
            for item in items:
                with ui.row().classes('w-full items-center gap-2 no-wrap hp-queue-item'):
                    ui.label(item.text).classes('flex-1 text-sm').style(
                        'overflow:hidden;text-overflow:ellipsis;white-space:nowrap'
                    )
                    ui.button(icon='edit', on_click=partial(_edit_item, item)).props(
                        'flat dense round size=sm'
                    ).tooltip('编辑')
                    ui.button(icon='close', on_click=partial(_remove_item, item)).props(
                        'flat dense round size=sm'
                    ).tooltip('删除')

    def _remove_item(item: QueuedMessage) -> None:
        """删除一条还没执行的排队消息（已出队执行的不在 queue 里，删不到）。"""
        if queue.remove(item):
            render_queue()

    def _edit_item(item: QueuedMessage) -> None:
        """编辑一条还没执行的排队消息（弹窗修改，保存后回写并重画）。"""
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('编辑待发送消息').classes('hp-card-title')
            editor = ui.textarea(value=item.text).props('autogrow').classes('w-full')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('取消', on_click=dialog.close).props('flat')

                def save() -> None:
                    text = (editor.value or '').strip()
                    # 路径 1：改成空白 → 不覆盖原文（避免把一条消息编辑成空）。
                    if not text:
                        return
                    # 路径 2：交给队列统一校验总字符数；被拒就提示并留在弹窗里。
                    reason = queue.replace_text(item, text)
                    if reason is not None:
                        notify(reason, kind='warning')
                        return
                    # 路径 3：正常保存并重画队列。
                    dialog.close()
                    render_queue()

                ui.button('保存', on_click=save).props('unelevated')
        dialog.open()

    def refresh_stats() -> None:
        """按当前会话重算底栏统计：轮数/引用条数 + 上下文占用。"""
        entries = conversations.current().entries
        # 路径：分别估算用户问题、回答、引用证据的 token，再求和算占比。
        user_tokens = sum(chat_stats.estimate_tokens(e.question) for e in entries)
        answer_tokens = sum(chat_stats.estimate_tokens(e.turn.answer) for e in entries)
        ref_tokens = sum(
            chat_stats.estimate_tokens(f.text) for e in entries for f in e.turn.fragments
        )
        total = user_tokens + answer_tokens + ref_tokens
        share = chat_stats.context_share(total)

        turns_label.text = f'{len(entries)} 轮'
        refs_label.text = f'{sum(len(e.turn.fragments) for e in entries)} 条引用'
        total_label.text = (
            f'{chat_stats.format_tokens(total)} / '
            f'{chat_stats.format_tokens(chat_stats.CONTEXT_LIMIT)}（{share:.0%}）'
        )
        # 三段宽度按各自 token 占比设置（超出窗口时由 bar 的 overflow 剪掉）。
        user_seg.style(f'width: {_seg_width(user_tokens)}')
        answer_seg.style(f'width: {_seg_width(answer_tokens)}')
        ref_seg.style(f'width: {_seg_width(ref_tokens)}')

    # 括号化多个 with（Python 3.10+）：外层是底栏容器，内层是限宽内容区，
    # 块里的元素都进**最内层**（hp-measure），一次搭出两层嵌套。
    with (
        ui.column().classes('w-full hp-composer'),
        ui.column().classes('w-full hp-measure px-4 py-3'),
    ):
        # 输入框上方的附件 chip（空时不占空间）。
        attach_box = ui.column().classes('w-full gap-1')
        # 输入框上方的队列区（空队列时什么都不画）。
        queue_box = ui.column().classes('w-full gap-1')
        # 输入行：附件按钮 + 多行输入框 + 发送按钮（检索期间也保持可用）。
        with ui.row().classes('w-full items-end gap-1 hp-composer-box'):
            with ui.element('div').classes('hp-attach-wrap'):
                # 以后这里可以扩成“+”菜单：附件 / 斜杠指令 / 快捷键。
                ui.button(icon='add').props('flat dense round size=sm').classes(
                    'hp-attach-btn'
                ).tooltip('添加附件（以后可接快捷键）')
                # 透明覆盖在上面的上传器：点图标即打开系统文件框，不占布局。
                ui.upload(
                    multiple=True,
                    auto_upload=True,
                    max_files=5,
                    max_file_size=attachment_service.MAX_BYTES,
                    on_upload=_on_upload,
                    on_rejected=lambda *_: notify(
                        f"单个附件不能超过 {attachment_service.MAX_BYTES // 1024 // 1024}MB",
                        kind="negative",
                    ),
                ).props('flat dense').classes('hp-attach-overlay').mark('chat-upload')
            box = (
                ui.textarea(placeholder='问点什么…（Enter 发送，Shift+Enter 换行）')
                .props('autogrow borderless dense autofocus')
                .classes('flex-1')
                .mark('chat-input')  # 界面测试用锚点（见 tests/ui/）
            )
            send_button_holder["button"] = ui.button(icon='send', on_click=_on_send_click).props(
                'round unelevated'
            ).classes('hp-send').mark('chat-send').tooltip('发送；生成中点击停止')
        # 统计行：轮数 / 引用条数 / 上下文占用（居中；点上下文数字展开分块明细）。
        with ui.row().classes('w-full items-center justify-center gap-3 hp-composer-stats'):
            turns_label = ui.label('0 轮').classes('hp-stat-pill').mark('chat-stats-turns')
            refs_label = ui.label('0 条引用').classes('hp-stat-pill').mark('chat-stats-refs')
            with ui.row().classes('items-center gap-1 no-wrap'):
                ui.label('上下文').classes('hp-hint').tooltip(
                    '按 1M 窗口估算（与上下文层同源）；点数字可看分块占用'
                )
                with ui.element('span').classes('hp-context-bar'):
                    user_seg = ui.element('span').classes('hp-context-user')
                    answer_seg = ui.element('span').classes('hp-context-answer')
                    ref_seg = ui.element('span').classes('hp-context-ref')
                total_label = ui.label('0 / 1M（0%）').classes('hp-stat-pill cursor-pointer').mark(
                    'chat-stats-context'
                )
                # 上下文明细：**小弹窗**（点数字就在它旁边弹出，不再撑开整行占高度）。
                detail_menu = ui.menu().classes('hp-context-pop')
                with detail_menu:
                    detail_holder = ui.column().classes('gap-1').style('max-width: 380px;')

        def _open_context_detail() -> None:  # 点数字弹出上下文明细
            """按最近一轮的 context_usage 渲染到小弹窗里。"""
            entries = conversations.current().entries  # 当前会话轮次
            payload = entries[-1].turn.context_usage if entries else {}  # 最近一轮占用
            _render_block_detail(detail_holder, dict(payload or {}))  # 渲染明细
            detail_menu.open()  # 在点击位置（数字旁边）弹出

        total_label.on('click', _open_context_detail)  # 绑定点击

    refs = streaming.StreamRefs(  # 发送/消费要用的引用（显式传，不用全局）
        live_state=live_state,  # 本页实时状态
        stream=stream,  # 对话流容器
        scroll=scroll,  # 滚动区
        box=box,  # 输入框
        queue=queue,  # 待发送队列
        busy=busy,  # worker 标记
        active=active,  # 当前流
        pending_files=pending_files,  # 待发送附件
        sync_button=_sync_button,  # 同步按钮
        render_queue=render_queue,  # 重画排队区
        render_pending_files=_render_pending_files,  # 重画附件
        refresh_stats=refresh_stats,  # 重算统计
        send_button=lambda: send_button_holder.get('button'),  # 取按钮
        on_refresh=on_refresh,  # 收成历史轮
    )

    # Enter 发送：`.exact` 让 Shift+Enter 保持换行；`.prevent` 阻止回车插入换行——
    # 否则换行会触发一次 input 事件，把刚清空的输入框又填回原文（服务端清空被覆盖）。
    box.on('keydown.enter.exact.prevent', send)
    return refresh_stats
