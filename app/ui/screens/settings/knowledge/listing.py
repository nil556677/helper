"""知识库 · 视图二：资料列表。

一份资料一行；筛选/排序/分页全在服务端；批量动作（重建 / 重新向量化 / 导出 / 删除）。
**点名字或「详情」进详情页**（详情是本包的第三个视图，本模块不认识它，只回调 `open_doc`）。

**为什么用类而不是一个大函数**（R2/R3）：`render` 曾任性地一个函数画整页（约 290 行）。
改成 `_List` 类后，**每个方法只干一件事**：建控件、取数据、画分页、画行、清空态、
各批量动作，都各占一个短方法——看代码不用滑屏。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from nicegui import run, ui

from app.services import knowledge as knowledge_service
from app.ui.components.dialog import confirm, notify
from app.ui.components.format import human_size
from app.ui.components.pager import PageState, render_pager
from app.ui.screens.settings.knowledge.widgets import (
    open_right_preview,
    parse_summary,
    status_chip,
    status_label,
)


class _List:
    """资料列表的控件与行为（每个方法一件事）。"""

    def __init__(
        self,
        container: ui.column,
        *,
        state: PageState,
        filters: dict[str, str],
        picked: set[int],
        open_doc: Any,
    ) -> None:
        self.container = container
        self.state = state
        self.filters = filters
        self.picked = picked
        self.open_doc = open_doc
        self.seen: dict[str, Any] = {"pager": None, "rows": None}
        self.page_ids: list[int] = []

        self.count_label: ui.label | None = None
        self.pick_label: ui.label | None = None
        self.pick_bar: ui.row | None = None
        self.list_box: ui.column | None = None
        self.empty: ui.label | None = None
        self.empty_action: ui.button | None = None
        self.pager_box: ui.column | None = None

        self._build()
        self.refresh(force=True)

    # ---- 取数 ----

    def _fetch(self) -> Any:
        return knowledge_service.documents_page(
            page=self.state.page,
            size=self.state.size,
            query=self.filters["query"],
            status=self.filters.get("status") or None,
            sort=self.filters.get("sort", "updated_at"),
            order=self.filters.get("order", "desc"),
        )

    # ---- 刷新（总入口）----

    def refresh(self, *, force: bool = False) -> None:
        page = self._fetch()
        self.state.total = int(page.total)
        self.state.page = int(page.page)
        self.count_label.text = f"{len(page.items)}/{page.total} 份" if page.total else "0 份"
        self.count_label.update()
        self.pick_label.text = f"已选 {len(self.picked)} 份"
        self.pick_label.update()
        self.pick_bar.set_visibility(bool(self.picked))
        self.page_ids[:] = [item.id for item in page.items]

        self._draw_pager(page, force)
        self._draw_rows(page, force)
        self._fill_empty(page)

    def _draw_pager(self, page: Any, force: bool) -> None:
        """分页控件：只有页码/总数/每页条数变了才重画（否则下拉会在用户点开时被抽走）。"""
        mark = (self.state.total, self.state.page, self.state.size)
        if not force and mark == self.seen["pager"]:
            return
        self.seen["pager"] = mark
        self.pager_box.clear()
        with self.pager_box:
            if page.total:
                render_pager(self.state, on_change=self.refresh, noun="份")

    def _draw_rows(self, page: Any, force: bool) -> None:
        """行内容：筛选/页码/选中态/行数据任一变了才重画。"""
        rows_mark = (
            self.state.page,
            self.state.size,
            self.filters["query"],
            self.filters.get("status", ""),
            self.filters.get("sort", "updated_at"),
            self.filters.get("order", "desc"),
            tuple(sorted(self.picked)),
            tuple((item.id, item.chunks, item.status, item.detail) for item in page.items),
        )
        if not force and rows_mark == self.seen["rows"]:
            return
        self.seen["rows"] = rows_mark
        self.list_box.clear()
        with self.list_box:
            if page.total:
                self._draw_header()
            for item in page.items:
                self._row(item)
        self.empty_action.set_visibility(
            not page.total and bool(self.filters["query"] or self.filters.get("status"))
        )

    def _draw_header(self) -> None:
        with ui.row().classes("hp-row-head w-full items-center gap-2"):
            ui.element("div").classes("hp-col-check")
            ui.label("名称").classes("hp-col-name")
            ui.label("大小").classes("hp-col-size hp-col-hide-md")
            ui.label("片段").classes("hp-col-chunks hp-col-hide-md").tooltip(
                "可检索的子块数；父块是上下文分组，不单独检索"
            )
            ui.label("状态").classes("hp-col-status")
            ui.label("操作").classes("hp-col-actions")

    def _fill_empty(self, page: Any) -> None:
        """空状态分两种：筛选筛没了 vs 一份没导（文案与动作不同）。"""
        if page.total:
            self.empty.text = ""
        elif self.filters["query"] and self.filters.get("status"):
            self.empty.text = (
                f"没有名称含「{self.filters['query']}」且状态为"
                f"「{status_label(self.filters['status'])}」的资料。"
            )
        elif self.filters["query"]:
            self.empty.text = f"没有名称含「{self.filters['query']}」的资料。"
        elif self.filters.get("status"):
            self.empty.text = f"没有状态为「{status_label(self.filters['status'])}」的资料。"
        else:
            self.empty.text = "还没有资料——去「导入」页选文件试试。"
        self.empty.update()

    # ---- 建控件（只建一次；刷新只重画 list_box / pager_box）----

    def _build(self) -> None:
        with self.container, ui.column().classes("hp-card w-full gap-2 hp-list-card hp-kb-list"):
            self._build_card_head()
            self._build_toolbar()
            self._build_pick_bar()
            self.list_box = ui.column().classes("w-full gap-0")
            self.empty = ui.label("").classes("hp-field-help")
            self.empty_action = (
                ui.button("清空筛选", icon="filter_alt_off", on_click=lambda: self._clear_filters())
                .props("flat dense no-caps")
                .classes("hp-row-button w-auto")
                .mark("kb-clear-filters")
            )
            self.empty_action.set_visibility(False)
            with ui.column().classes("hp-list-footer w-full"):
                self.pager_box = ui.column().classes("w-full gap-0")

    def _build_card_head(self) -> None:
        with ui.row().classes("hp-card-head w-full no-wrap"):
            ui.icon("library_books").classes("text-base hp-card-icon")
            ui.label("资料").classes("hp-card-title")
            self.count_label = ui.label("0").classes("hp-field-help")
            ui.space()

    def _build_toolbar(self) -> None:
        with ui.row().classes("hp-toolbar w-full items-center gap-2"):
            ui.input(
                placeholder="按名称筛选…",
                value=self.filters["query"],
                on_change=lambda event: self._search(str(event.value or "")),
            ).props("outline dense clearable").classes("hp-search").mark("kb-search")
            ui.select(
                {
                    "": "全部状态",
                    "pending": "待处理",
                    "indexing": "处理中",
                    "ready": "就绪",
                    "failed": "失败",
                },
                value=self.filters.get("status", ""),
                on_change=lambda event: self._filter_changed("status", str(event.value or "")),
            ).props("outline dense options-dense").classes("hp-filter").mark("kb-status")
            ui.select(
                {
                    "updated_at": "按时间",
                    "name": "按名称",
                    "size": "按大小",
                    "chunks": "按片段数",
                    "status": "按状态",
                },
                value=self.filters.get("sort", "updated_at"),
                on_change=lambda event: self._filter_changed("sort", str(event.value or "updated_at")),
            ).props("outline dense options-dense").classes("hp-filter").mark("kb-sort")
            ui.button(
                icon="swap_vert",
                on_click=lambda: self._filter_changed(
                    "order", "asc" if self.filters.get("order", "desc") == "desc" else "desc"
                ),
            ).props("flat dense round").mark("kb-order").tooltip("切换升序/降序")
            ui.space()
            ui.button("全选本页", icon="select_all", on_click=lambda: self._select_page()).props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto").mark("kb-select-page")

    def _build_pick_bar(self) -> None:
        # 批量动作条：选中才出现（见 §13.8 的列表页规范）
        with ui.row().classes("hp-actions w-full hp-pick-bar") as self.pick_bar:
            self.pick_label = ui.label("").classes("hp-field-help")
            ui.button("批量重建", icon="refresh", on_click=lambda: self._batch_rebuild()).props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto").mark("kb-batch-rebuild")
            ui.button("重新向量化", icon="memory", on_click=lambda: self._batch_revectorize()).props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto").mark("kb-batch-embed")
            ui.button("导出", icon="ios_share", on_click=lambda: self._batch_export()).props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto").mark("kb-batch-export")
            ui.button("批量删除", icon="delete_outline", on_click=lambda: self._batch_delete()).props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto").mark("kb-batch-delete")
            ui.button("取消选择", icon="deselect", on_click=lambda: self._clear_picks()).props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto").mark("kb-batch-clear")

    # ---- 筛选 / 选择 ----

    def _search(self, text: str) -> None:
        self.filters["query"] = text
        self.state.reset()
        self.picked.clear()
        self.refresh(force=True)

    def _filter_changed(self, key: str, value: str) -> None:
        self.filters[key] = value
        self.state.page = 1
        self.refresh(force=True)

    def _clear_filters(self) -> None:
        self.filters["query"] = ""
        self.filters["status"] = ""
        self.state.page = 1
        self.refresh(force=True)

    def _toggle_pick(self, doc_id: int) -> None:
        self.picked.discard(doc_id) if doc_id in self.picked else self.picked.add(doc_id)
        self.refresh(force=True)

    def _clear_picks(self) -> None:
        self.picked.clear()
        self.refresh(force=True)

    def _select_page(self) -> None:
        self.picked.update(self.page_ids)
        self.refresh(force=True)

    # ---- 批量动作 ----

    def _batch_rebuild(self) -> None:
        try:
            count = knowledge_service.enqueue_rebuild_many(sorted(self.picked))
        except ValueError as exc:  # 选中的里有"没有副本"的资料：明确告诉用户，不排必败任务
            notify(str(exc), kind="warning")
            return
        notify(f"已排队重建 {count} 份（任务页可见进度）", kind="positive")
        self.picked.clear()
        self.refresh(force=True)

    def _batch_revectorize(self) -> None:
        try:
            count = knowledge_service.revectorize(sorted(self.picked))
        except ValueError as exc:
            notify(str(exc), kind="warning")
            return
        notify(f"已排 {count} 份资料的向量化任务（任务页可见进度）", kind="positive")
        self.refresh(force=True)

    def _batch_export(self) -> None:
        if not self.picked:
            return
        target = Path.home() / "Downloads" / "知识库导出.zip"
        report = knowledge_service.export(sorted(self.picked), target)
        notify(
            f"已导出 {report['documents']} 份（{report['files']} 个副本）→ {report['path']}",
            kind="positive",
        )

    def _batch_delete(self) -> None:
        count = len(self.picked)

        async def do() -> None:
            """删除放线程池：DB 级联删 + 删副本文件，别卡界面。"""
            picked = sorted(self.picked)
            try:
                removed = await run.io_bound(knowledge_service.delete, picked)
                notify(f"已删除 {removed} 份资料", kind="positive")
            except Exception as exc:
                notify(f"删除失败：{exc}", kind="negative")
            self.picked.clear()
            self.refresh(force=True)

        confirm(
            f"删掉选中的 {count} 份资料？",
            "它们的片段与索引会一起删掉，副本文件也会清掉。",
            do,
            confirm_label="删掉",
            danger=True,
            confirm_mark="kb-batch-confirm",
        )

    # ---- 单行 ----

    def _row(self, item: knowledge_service.DocumentView) -> None:
        """一行：勾选 + 名称（点进详情）+ 大小/片段/状态 + 四个动作。"""
        failed = item.status == "failed"
        chosen = item.id in self.picked
        with ui.row().classes("hp-row-item w-full items-center gap-2"):
            with ui.element("div").classes("hp-col-check"):
                ui.button(
                    icon="check_box" if chosen else "check_box_outline_blank",
                    on_click=lambda doc_id=item.id: self._toggle_pick(doc_id),
                ).props("flat dense round size=sm").classes("hp-pick").mark(
                    f"kb-pick-{item.id}"
                ).tooltip("取消选中" if chosen else "选中")
            with ui.column().classes("hp-col-name gap-0"):
                name_label = ui.label(item.name).classes("hp-col-name hp-clickable")
                name_label.on(
                    "click", lambda _e, doc_id=item.id: self.open_doc("detail", doc_id)
                )
                name_label.tooltip("点开看这份资料的总览与分块")
                summary = parse_summary(item)
                if summary:
                    ui.label(summary).classes("hp-field-help")
                if failed and item.detail:
                    with ui.expansion("查看失败原因").classes("hp-field-help w-full"):
                        ui.label(item.detail).classes("hp-field-help")
            ui.label(human_size(item.size)).classes("hp-col-size hp-col-hide-md")
            ui.label("—" if failed else str(item.chunks)).classes("hp-col-chunks hp-col-hide-md")
            with ui.element("div").classes("hp-col-status"):
                status = status_chip(
                    item.status, done=item.vectors_done, total=item.vectors_total
                )
                if failed:
                    status.tooltip("这份没能用上；可以删掉它再换一份")
                elif item.status == "indexing":
                    status.tooltip("文字已经能搜到，向量索引还在后台建（任务页可见进度）")
            with ui.element("div").classes("hp-col-actions"):
                ui.button(
                    icon="open_in_new", on_click=lambda doc_id=item.id: self.open_doc("detail", doc_id)
                ).props("flat dense round size=sm").classes("hp-icon-view hp-btn-icon").mark(
                    f"kb-open-{item.id}"
                ).tooltip("看详情")
                ui.button(
                    icon="preview", on_click=lambda doc_id=item.id: open_right_preview(doc_id)
                ).props("flat dense round size=sm").classes("hp-icon-button hp-btn-icon").mark(
                    f"kb-preview-{item.id}"
                ).tooltip(
                    "右栏快速预览原件"
                )
                if item.status not in ("pending", "failed"):
                    # pending=还没导入完、failed=副本已丢：两者都没有可重建的副本，
                    # 显示按钮只会换来"找不到副本"，干脆不给入口（删除后重导即可）。
                    ui.button(icon="refresh", on_click=lambda: self._rebuild(item)).props(
                        "flat dense round size=sm"
                    ).classes("hp-icon-warn hp-btn-icon").mark(f"kb-rebuild-{item.id}").tooltip(
                        "重建：用副本重新解析与切分"
                    )
                ui.button(
                    icon="delete_outline", on_click=lambda: self._ask_delete(item)
                ).props("flat dense round size=sm").classes("hp-icon-danger hp-btn-icon").mark(
                    f"kb-delete-{item.id}"
                ).tooltip(
                    "从知识库里删掉这一份"
                )

    def _rebuild(self, item: knowledge_service.DocumentView) -> None:
        try:
            job_id = knowledge_service.enqueue_rebuild(int(item.id))
        except ValueError as exc:  # 没有副本（pending/failed）——给原因，不排必败任务
            notify(str(exc), kind="warning")
            return
        notify(f"「{item.name}」已加入重建队列（任务 #{job_id}）", kind="positive")
        self.refresh(force=True)

    def _ask_delete(self, item: knowledge_service.DocumentView) -> None:
        async def do() -> None:
            try:
                removed = await run.io_bound(knowledge_service.delete, [int(item.id)])
                notify(
                    f"已删除「{item.name}」" if removed else f"「{item.name}」已经不在库里了",
                    kind="positive" if removed else "info",
                )
            except Exception as exc:
                notify(f"删除失败：{exc}", kind="negative")
            self.refresh(force=True)

        confirm(
            f"删掉「{item.name}」？",
            "它的片段与索引会一起删掉，副本文件也会清掉。",
            do,
            confirm_label="删掉",
            danger=True,
            confirm_mark="kb-delete-confirm",
        )


def render(
    container: ui.column,
    *,
    state: PageState,
    filters: dict[str, str],
    picked: set[int],
    open_doc: Any,
) -> Callable[..., None]:
    """画列表，返回它的 `refresh`。"""
    view = _List(container, state=state, filters=filters, picked=picked, open_doc=open_doc)
    return view.refresh
