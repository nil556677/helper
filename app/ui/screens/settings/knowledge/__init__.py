"""设置 · 知识库（三个页签：总览 / 资料列表 / 导入；点记录进详情）。

**为什么拆成包**：这个界面曾经是一个 1000 行的单文件——总览、列表、导入、详情、上传
全挤在一起，改一处要滑半天。现在**一页一个模块**，只共享 `widgets`：

| 模块 | 职责 |
| --- | --- |
| :mod:`overview` | 总览页（状态一眼看全） |
| :mod:`listing` | 资料列表（筛选/排序/批量/分页） |
| :mod:`importing` | 导入页（上传 + 三步说明） |
| :mod:`detail` | 资料详情页（该份总览 + 分块 + 精确字段 + 原文 + 版本） |
| :mod:`widgets` | 共用小件（卡片头/统计格/状态徽标/解析摘要） |

本模块只做**导航与状态**：三个页签 + 视图切换（清空重画，不引路由）。
列表的筛选/页码/勾选存在这里，**从详情返回列表不丢状态**。
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from app.services import knowledge as knowledge_service  # noqa: F401 - 供子模块共用同一服务引用
from app.ui.components.auto_refresh import AutoRefresh
from app.ui.components.pager import PAGE_SIZES, PageState
from app.ui.screens.settings.knowledge import detail, importing, listing, overview

#: 总览/列表的后台自查间隔（秒）——内容真变了才重画
AUTO_REFRESH_S = 2.0


def render() -> None:
    """画「知识库」分区（三个页签 + 详情视图 + 共享状态）。"""
    state = PageState(size=PAGE_SIZES[0])
    filters: dict[str, str] = {"query": "", "status": "", "sort": "updated_at", "order": "desc"}
    picked: set[int] = set()
    current: dict[str, Any] = {"refresh": None}
    auto = AutoRefresh(interval_s=AUTO_REFRESH_S)

    def auto_refresh() -> None:
        """自动刷新只刷新**当前视图**（切走后旧容器已清空）。"""
        refresh = current.get("refresh")
        if callable(refresh):
            refresh()

    guard: dict[str, bool] = {"busy": False}

    with ui.column().classes("w-full gap-4"):
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            ui.label("知识库").classes("hp-page-title")
            ui.space()
            with (
                ui.tabs(
                    value="overview",
                    on_change=lambda event: _tab_changed(str(event.value)),
                )
                .props("no-caps dense align=left")
                .classes("hp-tabs") as tabs
            ):
                ui.tab("overview", "总览", icon="insights").mark("kb-tab-overview")
                ui.tab("list", "资料列表", icon="format_list_bulleted").mark("kb-tab-list")
                ui.tab("import", "导入", icon="upload_file").mark("kb-tab-import")
            auto_box = ui.row().classes("items-center gap-2 no-wrap")
        content = ui.column().classes("w-full gap-4")

    def _tab_changed(value: str) -> None:
        """页签切换（详情页也会把页签停在「资料列表」）。"""
        if guard["busy"]:
            return
        show(value)

    def show(mode: str, doc_id: int | None = None) -> None:
        """切换视图：清空容器重画，并把新视图的 refresh 交给自动刷新。"""
        guard["busy"] = True
        try:
            target = "list" if mode == "detail" else mode
            if tabs.value != target:
                tabs.set_value(target)
            content.clear()
            current["refresh"] = None
            if mode == "list":
                current["refresh"] = listing.render(
                    content, state=state, filters=filters, picked=picked, open_doc=show
                )
            elif mode == "import":
                importing.render(content, go_list=lambda: show("list"))
            elif mode == "detail":
                current["refresh"] = detail.render(
                    content, int(doc_id or 0), back=lambda: show("list")
                )
            else:
                current["refresh"] = overview.render(content, open_doc=show)
        finally:
            guard["busy"] = False

    with auto_box:
        auto.render(auto_refresh)
    show("overview")


__all__ = ["render"]
