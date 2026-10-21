"""知识库 · 视图一：总览（独立页签）。

只回答"我现在是什么状态"：统计六格、向量覆盖率、最近导入、失败速览、索引过期提示。
**导入入口不在这里**——它在「导入」页（用户定的：上传单独一页）。

**为什么不写成一个 render 长函数**（R2）：总览的四块（索引提示/统计/最近/失败）各自独立，
互相不知道对方。写成四个**模块级**函数（各自拿自己的 box 和 `seen`），`render` 只负责
"搭四个容器 + 按序调用"，这样每块都好读、好测、好单独改。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

from app.services import knowledge as knowledge_service
from app.services import settings as settings_service
from app.ui.components.dialog import notify
from app.ui.components.format import human_size
from app.ui.screens.settings.knowledge.widgets import card_head, mini_row, stat


def render(container: ui.column, *, open_doc: Any) -> Callable[..., None]:
    """画总览，返回它的 `refresh`（AutoRefresh 会调它）。"""
    seen: dict[str, Any] = {"banner": None, "stats": None, "recent": None, "failed": None}

    with container:
        banner = ui.column().classes("w-full gap-0")
        stats_box = ui.column().classes("hp-card w-full gap-3")
        recent_box = ui.column().classes("hp-card w-full gap-2")
        failed_box = ui.column().classes("hp-card w-full gap-2")

    def refresh(*, force: bool = False) -> None:
        _banner(banner, seen, force)
        _stats(stats_box, seen, force)
        _recent(recent_box, seen, open_doc, force)
        _failed(failed_box, seen, open_doc, force)

    refresh(force=True)
    return refresh


def _banner(banner: ui.column, seen: dict[str, Any], force: bool) -> None:
    """索引过期提示条：只在指纹对不上时出现，点任一条即记指纹消失。"""
    status = settings_service.index_status()
    attrs_stale = bool(status.get("attrs_stale"))
    mark = (status["action"], attrs_stale, tuple(status["changed_labels"]))
    if not force and mark == seen["banner"]:
        return
    seen["banner"] = mark
    banner.clear()
    if status["action"] == "none" and not attrs_stale:
        return
    with (
        banner,
        ui.card().classes("hp-card w-full hp-index-banner"),
        ui.row().classes("items-center gap-2 w-full no-wrap"),
    ):
        ui.icon("sync_problem").classes("text-base")
        with ui.column().classes("gap-0"):
            ui.label("索引和当前参数不一致").classes("hp-card-title")
            ui.label(str(status["detail"])).classes("hp-field-help")
        ui.space()
        if status["action"] == "lexical":
            ui.button("只重算词法", icon="bolt", on_click=_rebuild_lexical).props(
                "flat dense no-caps"
            ).mark("kb-rebuild-lexical").tooltip("不重新解析、不动向量，秒级完成")
        if attrs_stale:
            ui.button("回填动态字段", icon="rule", on_click=_backfill_attrs).props(
                "flat dense no-caps"
            ).mark("kb-backfill-attrs").tooltip("按当前字段字典重算属性，不重嵌（秒级）")
        ui.button("全量重建", icon="restart_alt", on_click=_rebuild_all_index).props(
            "flat dense no-caps"
        ).mark("kb-rebuild-all").tooltip("重新解析 + 重新切分 + 重建向量（小时级，走队列）")


def _stats(stats_box: ui.column, seen: dict[str, Any], force: bool) -> None:
    """统计六格 + 向量覆盖率。"""
    data = knowledge_service.stats()
    mark = (
        data.documents,
        data.chunks,
        data.bytes,
        data.updated_at,
        data.vector_ratio,
        data.indexing,
        data.failed,
    )
    if not force and mark == seen["stats"]:
        return
    seen["stats"] = mark
    stats_box.clear()
    with stats_box:
        card_head("insights", "概览", "一眼看全")
        with ui.element("div").classes("hp-stats w-full"):
            stat(str(data.documents), "份资料")
            stat(str(data.chunks), "个片段")
            stat(human_size(int(data.bytes)), "副本占用")
            # 向量后端不可用时这几个数没有意义：显示"—"，别用 0 误导（也避免每 2 秒抛 500）
            stat("—" if data.vector_error else human_size(int(data.vector_bytes)), "向量占用")
            stat(str(data.failed), "失败")
            stat(str(data.indexing), "处理中")
        if data.vector_error:
            ui.label(f"向量库暂不可用：{data.vector_error}（其他统计不受影响）").classes(
                "hp-field-help"
            )
        elif data.vectors_total:
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                ui.linear_progress(
                    value=data.vector_ratio / 100, show_value=False, size="6px"
                ).classes("flex-1")
                ui.label(f"向量覆盖 {data.vector_ratio}%").classes("hp-field-name")
            ui.label(
                f"已向量化 {data.vectors_done:,}/{data.vectors_total:,} 块"
                + ("（后台正在补，任务页可见进度）" if data.vector_ratio < 100 else "")
            ).classes("hp-field-help")


def _recent(
    recent_box: ui.column, seen: dict[str, Any], open_doc: Any, force: bool
) -> None:
    """最近导入 5 条（点进详情）。"""
    page = knowledge_service.documents_page(page=1, size=5, sort="updated_at", order="desc")
    mark = tuple((item.id, item.status) for item in page.items)
    if not force and mark == seen["recent"]:
        return
    seen["recent"] = mark
    recent_box.clear()
    with recent_box:
        with ui.row().classes("hp-card-head w-full items-center gap-2"):
            ui.icon("schedule").classes("text-base hp-card-icon")
            ui.label("最近导入").classes("hp-card-title")
            ui.space()
            ui.label(f"{page.total} 份").classes("hp-field-help")
            ui.button(
                "全部", icon="chevron_right", on_click=lambda: open_doc("list")
            ).props("flat dense no-caps").classes("hp-row-button w-auto")
        if not page.items:
            with ui.column().classes("hp-empty w-full"):
                ui.icon("inbox").classes("text-2xl")
                ui.label("还没有资料——先去导入一份，之后这里会显示概览。").classes("hp-field-help")
                ui.button(
                    "去导入资料", icon="upload_file", on_click=lambda: open_doc("import")
                ).props("unelevated dense no-caps").classes("hp-send w-auto").mark("kb-empty-import")
            return
        for item in page.items:
            mini_row(item, on_click=lambda doc_id=item.id: open_doc("detail", doc_id))


def _failed(failed_box: ui.column, seen: dict[str, Any], open_doc: Any, force: bool) -> None:
    """失败速览：带原因的 5 条，一条都没有就藏起来。"""
    page = knowledge_service.documents_page(page=1, size=5, status="failed")
    mark = tuple((item.id, item.detail) for item in page.items)
    if not force and mark == seen["failed"]:
        return
    seen["failed"] = mark
    failed_box.clear()
    if page.total == 0:
        failed_box.set_visibility(False)
        return
    failed_box.set_visibility(True)
    with failed_box:
        card_head("error_outline", "失败", f"{page.total} 份 · 点开看原因")
        for item in page.items:
            mini_row(
                item,
                on_click=lambda doc_id=item.id: open_doc("detail", doc_id),
                show_reason=True,
            )


def _rebuild_lexical() -> None:
    """只重算词法（秒级，同步做也没问题）。"""
    outcome = settings_service.rebuild_lexical()
    updated = int(outcome.get("updated") or 0)
    notify(f"词法重算完成：更新 {updated} 行" if updated else "没有需要重算的片段", kind="positive")


def _rebuild_all_index() -> None:
    """全量重建（走队列：小时级的活不能在界面线程里跑）。"""
    job_id = settings_service.rebuild_all()
    notify(f"已排队全量重建（任务 #{job_id}，任务页可见进度）", kind="positive")


def _backfill_attrs() -> None:
    """回填动态字段（只重算 attrs，不重新解析、不动向量；走队列）。"""
    job_id = knowledge_service.enqueue_backfill_attrs()
    notify(f"已排队回填动态字段（任务 #{job_id}，任务页可见进度）", kind="positive")
