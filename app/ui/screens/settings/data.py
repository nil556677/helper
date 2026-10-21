"""设置 · 数据与存储：数据目录（备份入口）+ 向量子区（Qdrant 集合 / 引擎）。

把原「通用」里的"数据"卡拿出来，与「向量」并成一个分区：这里回答的是同一件事——
"资料副本、数据库、向量各自存在哪、怎么管"。左栏少一项，主区没之前那么碎。

版式：一张卡一件事。数据目录卡讲"备份=整个目录"；向量子区沿用 `vector` 的两张卡
（向量数据 / Qdrant 控制台），只把 `vector.render()` 接到本分区里。
"""

from __future__ import annotations

from functools import partial

from nicegui import ui

from app.services import settings as settings_service
from app.ui.components.card import card_head
from app.ui.components.dialog import notify


def render() -> None:
    """画「数据与存储」分区（本地 import：避免与 settings 包的循环 import）。"""
    from app.ui.screens.settings import vector

    overview = settings_service.data_overview()

    def open_it(target: str, what: str) -> None:
        if settings_service.open_folder(target):
            return
        if settings_service.copy_path(target):
            notify(
                f"资源管理器打不开{what}；路径已复制，粘贴到地址栏即可打开",
                kind="warning",
            )
        else:
            notify(f"打不开{what} —— 它可能已被删掉或移到别处", kind="negative")

    with ui.column().classes("w-full gap-4"):
        with ui.column().classes("hp-page-head gap-1"):
            ui.label("数据与存储").classes("hp-page-title")

        # ---- 数据目录（备份的入口）----
        with ui.column().classes("hp-card w-full gap-3"):
            card_head("folder_open", "数据目录")
            with ui.row().classes("hp-field-row"):
                with ui.column().classes("hp-field-main gap-0"):
                    ui.label("数据目录").classes("hp-field-name")
                    ui.label("备份 = 复制这个目录：资料、数据库、设置都在里面。").classes(
                        "hp-field-help"
                    )
                ui.label(overview.data_dir).classes("hp-path")
            with ui.row().classes("hp-actions"):
                ui.button(
                    "打开数据目录",
                    icon="folder_open",
                    on_click=partial(open_it, overview.data_dir, "数据目录"),
                ).props("flat dense no-caps").classes("hp-row-button w-auto").mark("data-open-dir")
                ui.button(
                    "打开日志目录",
                    icon="description",
                    on_click=partial(open_it, overview.log_file, "日志目录"),
                ).props("flat dense no-caps").classes("hp-row-button w-auto").mark("data-open-log")
            with ui.expansion("详细信息（排查问题时用）", icon="bug_report").classes(
                "w-full text-sm hp-sources"
            ), ui.column().classes("w-full gap-2 p-2"):
                for spot in overview.spots:
                    _detail_row(spot.label, spot.path)
                _detail_row("数据结构版本", str(overview.schema_version))

    # 向量子区接在当前分区内容的下面（同一滚动区）
    vector.render()


def _detail_row(name: str, value: str) -> None:
    """折叠区里的一行：名称 + 值（值用等宽字体，便于对照路径）。"""
    with ui.row().classes("w-full items-baseline gap-3 no-wrap"):
        ui.label(name).classes("hp-field-name w-24 shrink-0")
        ui.label(value).classes("hp-path").style("text-align: left; max-width: none")
