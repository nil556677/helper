"""设置 · 知识库：导入资料 → 本地切分建索引 → 看着它、能删、能重建。

**为什么在设置里而不是主栏**：用户定的（DSH 也是这么放的）。主栏留给"日常要做的事"（聊天）。

**四块内容**：

1. **概览**：就绪份数 / 片段数 / 副本占用 / 最后更新——先回答"我现在是什么状态"；
2. **导入**：`ui.upload` 走系统文件对话框、可多选；**上传先落 `data/inbox/`，再排一条导入任务**
   （解析、切分、建索引由任务系统在后台做，进度在 设置 → 任务 里看）；
3. **资料**：一行一份，能看到片段数与状态（**失败的也列出来并写清原因**，只弹一次通知的话
   用户关掉就再也找不到"哪一份失败了、为什么"），能重建、能删；
4. **筛选与分页**（重设计）：名称筛选 + 服务端分页——资料可能几千份，一次全画出来又慢又没人看。

**为什么导入改成排队了**：一份 PDF 可能要几十秒，当场做就是让界面等着；排队之后
"导到一半关窗口"也变成"下次启动接着导"（任务表 + inbox 暂存文件，见技术方案 §13）。

**刷新只重画变了的东西**：页码/筛选变了才重画分页控件，行内容变了才重画行——否则每秒重建一次
会让"搜索框/每页条数"在用户操作时被抽走（焦点与下拉菜单都会断）。
"""

from __future__ import annotations

from typing import Any

from nicegui import events, run, ui

from app import tasks as tasks_app
from app.services import knowledge as knowledge_service
from app.ui.components.auto_refresh import AutoRefresh
from app.ui.components.dialog import confirm, notify
from app.ui.components.format import human_size, human_time
from app.ui.components.pager import PAGE_SIZES, PageState, render_pager

#: 单个文件的上传上限（NiceGUI 默认只有 1 MB，装不下一份 PDF）
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

#: 后台导入时的自查间隔（秒）——内容真变了才重画
AUTO_REFRESH_S = 1.0


def _card_head(icon: str, title: str, extra: str = "") -> None:
    """卡片头：图标 + 标题（+ 可选的状态/说明）。"""
    with ui.row().classes("hp-card-head w-full"):
        ui.icon(icon).classes("text-base hp-card-icon")
        ui.label(title).classes("hp-card-title")
        if extra:
            ui.space()
            ui.label(extra).classes("hp-status")


def _stat(value: str, name: str) -> None:
    """概览里的一格数字。"""
    with ui.column().classes("gap-0"):
        ui.label(value).classes("text-lg font-medium")
        ui.label(name).classes("hp-field-help")


def render() -> None:
    """画「知识库」分区的内容（真数据）。"""
    state = PageState(size=PAGE_SIZES[0])
    filters: dict[str, str] = {"query": ""}
    auto = AutoRefresh(interval_s=AUTO_REFRESH_S)

    with ui.column().classes("w-full gap-4"):
        ui.label("知识库").classes("hp-page-title")
        ui.label(
            "资料留在本机：导入后只在本地解析、切分、建索引，检索也在本机完成。"
        ).classes("hp-hint")

        overview = ui.column().classes("hp-card w-full gap-3")

        # 列表卡：卡头 → 工具栏 → 主体 → 页脚（与任务页**同一套骨架**）
        # 卡头与工具栏是**静态的**（只建一次）：放进"每次刷新都清空"的容器里会让输入时焦点丢失、
        # 下拉被抽走——踩过这个坑
        with ui.column().classes("hp-card w-full gap-2 hp-list-card hp-kb-list"):
            with ui.row().classes("hp-card-head w-full no-wrap"):
                ui.icon("library_books").classes("text-base hp-card-icon")
                ui.label("资料").classes("hp-card-title")
                count_label = ui.label("0").classes("hp-field-help")
                ui.space()
                auto_box = ui.row().classes("items-center gap-2 no-wrap")
            with ui.row().classes("hp-toolbar w-full items-center gap-2"):
                ui.input(
                    placeholder="按名称筛选…",
                    value=filters["query"],
                    on_change=lambda event: _search(str(event.value or "")),
                ).props("outline dense clearable").classes("hp-search").mark("kb-search")
                ui.space()
                ui.button("全选本页", icon="select_all", on_click=lambda: _select_page()).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("kb-select-page")
            # 批量动作条：选中才出现（见 §13.8 的列表页规范）
            with ui.row().classes("hp-actions w-full hp-pick-bar") as pick_bar:
                pick_label = ui.label("").classes("hp-field-help")
                ui.button("批量重建", icon="refresh", on_click=lambda: _batch_rebuild()).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("kb-batch-rebuild")
                ui.button("批量删除", icon="delete_outline", on_click=lambda: _batch_delete()).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("kb-batch-delete")
                ui.button("取消选择", icon="deselect", on_click=lambda: _clear_picks()).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("kb-batch-clear")
            list_box = ui.column().classes("w-full gap-0")
            empty = ui.label("").classes("hp-field-help")
            with ui.column().classes("hp-list-footer w-full"):
                pager_box = ui.column().classes("w-full gap-0")
            seen: dict[str, Any] = {"pager": None, "rows": None, "overview": None}
            picked: set[int] = set()  # 批量选择（行会被重画，所以状态放在这里）
            page_ids: list[int] = []

        def refresh(*, force: bool = False) -> None:
            """重画概览与资料列表（导入 / 删除 / 重建 / 翻页之后调）。"""
            data = knowledge_service.stats()
            auto.touch()  # 记一次"刚查过库"：时间戳与"自动刷新已关"的提示都靠它
            overview_mark = (data.documents, data.chunks, data.bytes, data.updated_at)
            if force or overview_mark != seen["overview"]:
                seen["overview"] = overview_mark
                overview.clear()
                with overview:
                    _card_head("insights", "概览")
                    with ui.row().classes("gap-10 flex-wrap"):
                        _stat(str(data.documents), "份资料")
                        _stat(str(data.chunks), "个片段")
                        _stat(human_size(int(data.bytes)), "副本占用")
                        _stat(human_time(data.updated_at, empty="还没导过"), "最后更新")

            page = knowledge_service.documents_page(
                page=state.page, size=state.size, query=filters["query"]
            )
            state.total = int(page.total)
            state.page = int(page.page)
            count_label.text = (
                f"{len(page.items)}/{page.total} 份" if page.total else "0 份"
            )
            count_label.update()
            # 批量动作条 + 记住本页有哪些（"全选本页"要用）
            pick_label.text = f"已选 {len(picked)} 份"
            pick_label.update()
            pick_bar.set_visibility(bool(picked))
            page_ids[:] = [item.id for item in page.items]

            # 分页控件：只有页码/总数/每页条数变了才重画（否则下拉会在用户点开时被抽走）
            pager_mark = (state.total, state.page, state.size)
            if force or pager_mark != seen["pager"]:
                seen["pager"] = pager_mark
                pager_box.clear()
                with pager_box:
                    if page.total:
                        render_pager(state, on_change=refresh, noun="份")

            rows_mark = (
                state.page,
                state.size,
                filters["query"],
                tuple(sorted(picked)),  # 选中态变了要重画（图标要跟着换）
                tuple(
                    (item.id, item.chunks, item.status, item.detail)
                    for item in page.items
                ),
            )
            if not force and rows_mark == seen["rows"]:
                return
            seen["rows"] = rows_mark
            list_box.clear()
            with list_box:
                if page.total:
                    with ui.row().classes("hp-row-head w-full items-center gap-3"):
                        ui.element("div").style("width: 28px")  # 选择列（和行里的图标按钮对齐）
                        ui.label("名称").classes("hp-col-name")
                        ui.label("大小").classes("hp-col")
                        ui.label("片段").classes("hp-col")
                        ui.label("状态").classes("hp-col")
                        ui.label("操作").classes("hp-col")
                for item in page.items:
                    _document_row(item, refresh, picked=picked, on_pick=_toggle_pick)
            empty.text = (
                ""
                if page.total
                else (
                    "没有名称匹配的资料。"
                    if filters["query"]
                    else "还没有资料——用上面的「选择文件…」导入一份试试。"
                )
            )
            empty.update()

        def _search(text: str) -> None:
            """名称筛选：回到第一页再查（换条件等于换了一套页）。"""
            filters["query"] = text
            state.reset()
            picked.clear()  # 换了视角就别留着看不见的选择
            refresh(force=True)

        # ---- 批量选择（与任务页同一套交互）----

        def _toggle_pick(doc_id: int) -> None:
            picked.discard(doc_id) if doc_id in picked else picked.add(doc_id)
            refresh(force=True)

        def _clear_picks() -> None:
            picked.clear()
            refresh(force=True)

        def _select_page() -> None:
            picked.update(page_ids)
            refresh(force=True)

        def _batch_rebuild() -> None:
            count = tasks_app.enqueue_rebuild_many(sorted(picked))
            notify(f"已排队重建 {count} 份（任务页可见进度）", kind="positive")
            picked.clear()
            refresh(force=True)

        def _batch_delete() -> None:
            """批量删除：破坏性，确认一次（副本文件与片段一起删）。"""
            count = len(picked)
            confirm(
                f"删掉选中的 {count} 份资料？",
                "它们的片段与索引会一起删掉，副本文件也会清掉。",
                lambda: (
                    knowledge_service.delete(sorted(picked)),
                    picked.clear(),
                    refresh(force=True),
                ),
                confirm_label="删掉",
                danger=True,
                confirm_mark="kb-batch-confirm",
            )

        def _document_row(
            item: knowledge_service.DocumentView,
            reload_page: Any,
            *,
            picked: set[int],
            on_pick: Any,
        ) -> None:
            """列表里的一行：选择 + 名称（失败时带原因）+ 片段数 + 状态 + 重建/删除。"""
            failed = item.status == "failed"
            chosen = item.id in picked
            with ui.row().classes("hp-row-item w-full items-center gap-3"):
                ui.button(
                    icon="check_box" if chosen else "check_box_outline_blank",
                    on_click=lambda doc_id=item.id: on_pick(doc_id),
                ).props("flat dense round size=sm").classes("hp-pick").mark(
                    f"kb-pick-{item.id}"
                ).tooltip("取消选中" if chosen else "选中")
                with ui.column().classes("hp-col-name gap-0"):
                    ui.label(item.name).classes("hp-col-name")
                    if failed and item.detail:
                        # 完整原因 hover 可看（行里只放一句）
                        ui.label(item.detail).classes("hp-field-help").tooltip(item.detail)
                ui.label(human_size(item.size)).classes("hp-col")
                ui.label("—" if failed else str(item.chunks)).classes("hp-col")
                status = ui.label("失败" if failed else "就绪").classes(
                    "hp-col hp-status " + ("hp-status-bad" if failed else "hp-status-ok")
                )
                if failed:
                    status.tooltip("这份没能用上；可以删掉它再换一份")
                with ui.row().classes("hp-col items-center gap-1 no-wrap justify-end"):
                    ui.button(icon="refresh", on_click=lambda: _rebuild(item, reload_page)).props(
                        "flat dense round size=sm"
                    ).mark(f"kb-rebuild-{item.id}").tooltip("重建：用副本重新解析与切分")
                    ui.button(
                        icon="delete_outline", on_click=lambda: _ask_delete(item, reload_page)
                    ).props("flat dense round size=sm").mark(f"kb-delete-{item.id}").tooltip(
                        "从知识库里删掉这一份"
                    )

        def _rebuild(item: knowledge_service.DocumentView, reload_page: Any) -> None:
            """重建**走队列**：重新解析是重活，直接在界面线程里跑会卡住界面。"""
            job_id = tasks_app.enqueue_rebuild(int(item.id))
            notify(f"「{item.name}」已加入重建队列（任务 #{job_id}）", kind="positive")
            reload_page(force=True)

        def _ask_delete(item: knowledge_service.DocumentView, reload_page: Any) -> None:
            """删除是破坏性操作——用统一确认框，别让人误点一下就没了。"""
            confirm(
                f"删掉「{item.name}」？",
                "它的片段与索引会一起删掉，副本文件也会清掉。",
                lambda: (
                    knowledge_service.delete([int(item.id)]),
                    reload_page(force=True),
                ),
                confirm_label="删掉",
                danger=True,
            )

        # ---- 导入（这一块不随列表刷新而重建：上传控件要保持自己的状态）----
        async def on_upload(event: events.UploadEventArguments) -> None:
            """上传 → 先落 inbox → 排一条导入任务（解析、切分、建索引都交给任务系统）。

            为什么要排队而不是当场做完：一份大 PDF 可能要几十秒，当场做会让界面等着；
            排队之后"导到一半关窗口"也变成"下次启动接着导"（任务表 + inbox 暂存文件）。
            """
            data = event.content.read()
            payload = await run.io_bound(knowledge_service.stage_upload, event.name, data)
            job_id = tasks_app.enqueue_import(
                payload, dedupe_key=knowledge_service.upload_dedupe_key(data)
            )
            notify(f"「{event.name}」已加入队列（任务 #{job_id}）", kind="positive")
            refresh(force=True)

        with ui.column().classes("hp-card w-full gap-3"):
            _card_head("upload_file", "导入资料")
            ui.label(
                f"支持 {knowledge_service.supported_formats()}；可以一次选多个文件。"
                f"扫描件（图片型 PDF）要 OCR 才能读，还没接。"
            ).classes("hp-field-help")
            ui.upload(
                label="选择文件…",
                multiple=True,
                auto_upload=True,
                max_file_size=MAX_UPLOAD_BYTES,
                on_upload=on_upload,
                on_rejected=lambda *_: notify(
                    f"单个文件不能超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB", kind="negative"
                ),
            ).props("flat dense color=primary").classes("hp-upload w-full").mark("kb-upload")
            ui.label(
                "导入在后台排队进行，进度看 设置 → 任务。重复内容会自动跳过。"
                f"片段上限 {knowledge_service.chunk_limit()} 字。"
            ).classes("hp-field-help")

        refresh(force=True)
        with auto_box:  # 自动刷新放工具栏右侧（回填到上面留好的位置，保证骨架顺序）
            auto.render(refresh)
