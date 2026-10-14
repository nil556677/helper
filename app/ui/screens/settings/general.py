"""设置 · 通用：日志级别 + 数据落在哪（备份）+ 版本。

"数据"与"关于"并进这里：它们各自撑不起一个分区，但都属于"最基础的那点事"。
版式：**一张卡一件事**，行内"左名称右控件"。
"""

from __future__ import annotations

from functools import partial

from nicegui import ui

from app.services import settings as settings_service


def _card_head(icon: str, title: str) -> None:
    """卡片头：图标 + 标题（各卡统一，避免每个卡片各写一遍）。"""
    with ui.row().classes("hp-card-head"):
        ui.icon(icon).classes("text-base hp-card-icon")
        ui.label(title).classes("hp-card-title")


def render() -> None:
    """画「通用」分区的内容。"""
    overview = settings_service.data_overview()
    level = ui.select(
        list(settings_service.LOG_LEVELS),
        value=settings_service.log_level(),
    ).props("outlined dense").classes("w-28")

    def save() -> None:
        try:
            settings_service.save_log_level(str(level.value))
        except OSError as exc:  # 配置文件不可写：说清楚，别静默失败
            ui.notify(f"保存失败：{exc}", type="negative")
            return
        ui.notify("已保存，日志级别立即生效", type="positive")

    def open_it(target: str, what: str) -> None:
        """打开目录。失败要说出来——静默失败就是"点了按钮什么都没发生"。"""
        if not settings_service.open_folder(target):
            ui.notify(f"打不开{what} —— 它可能已被删掉或移到别处", type="negative")

    with ui.column().classes("w-full gap-4"):
        ui.label("通用").classes("hp-page-title")
        ui.label("最基础的三件事：日志、数据放在哪、版本。").classes("hp-hint")

        # ---- 日志 ----
        with ui.column().classes("hp-card w-full gap-3"):
            _card_head("subject", "日志")
            with ui.row().classes("hp-field-row"):
                with ui.column().classes("hp-field-main gap-0"):
                    ui.label("日志级别").classes("hp-field-name")
                    ui.label("改完立即生效，不用重启。").classes("hp-field-help")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    level
                    ui.button("保存", on_click=save).props("unelevated dense no-caps").classes(
                        "hp-send"
                    )

        # ---- 数据 ----
        with ui.column().classes("hp-card w-full gap-3"):
            _card_head("folder_open", "数据")
            with ui.row().classes("hp-field-row"):
                with ui.column().classes("hp-field-main gap-0"):
                    ui.label("数据目录").classes("hp-field-name")
                    ui.label("备份 = 复制这个目录：资料、数据库、设置都在里面。").classes(
                        "hp-field-help"
                    )
                ui.label(str(overview["paths"]["数据目录"])).classes("hp-path")
            with ui.row().classes("hp-actions"):
                ui.button(
                    "打开数据目录",
                    icon="folder_open",
                    on_click=partial(open_it, str(overview["paths"]["数据目录"]), "数据目录"),
                ).props("flat dense no-caps").classes("hp-row-button w-auto")
                ui.button(
                    "打开日志目录",
                    icon="description",
                    on_click=partial(open_it, str(overview["paths"]["日志文件"]), "日志目录"),
                ).props("flat dense no-caps").classes("hp-row-button w-auto")
            # 绝对路径与"数据结构版本"收进折叠区：排障有用，摆在正文里既像开发术语又撑窄屏
            with ui.expansion("详细信息（排查问题时用）", icon="bug_report").classes(
                "w-full text-sm hp-sources"
            ):
                with ui.column().classes("w-full gap-2 p-2"):
                    for name in ("安装目录", "数据库", "日志文件", "资料副本"):
                        _detail_row(name, str(overview["paths"][name]))
                    _detail_row("数据结构版本", str(overview["schema_version"]))

        # ---- 关于 ----
        with ui.column().classes("hp-card w-full gap-2"):
            _card_head("info", "关于")
            with ui.row().classes("items-baseline gap-2"):
                ui.label("helper").classes("hp-brand")
                ui.label(f"v{settings_service.app_version()}").classes("hp-tag")
            ui.label(
                "本地桌面 AI 助手：资料在本机、检索在本机，只把命中的资料片段与问题交给云端大模型。"
            ).classes("hp-field-help")


def _detail_row(name: str, value: str) -> None:
    """折叠区里的一行：名称 + 值（值用等宽字体，便于对照路径）。"""
    with ui.row().classes("w-full items-baseline gap-3 no-wrap"):
        ui.label(name).classes("hp-field-name w-24 shrink-0")
        ui.label(value).classes("hp-path").style("text-align: left; max-width: none")
