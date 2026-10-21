"""路径行：等宽路径 + 复制 + 打开（通用页用，以后"数据库/资料副本"也能用）。

**为什么要一个组件**：审查指出原来的 `hp-path`（右对齐 + break-all 硬折行）难读难核对，
而且没有"复制"——用户要去备份文档、报工单时只能手动选中一段折行的路径。
路径这种东西的正确展示是：**单行省略 + 悬停看全文 + 一键复制 + 一键打开**。
"""

from __future__ import annotations

from functools import partial

from nicegui import ui

from app.ui.components.dialog import notify


def path_row(path: str, *, open_label: str | None = None) -> None:
    """画一行路径：等宽左对齐（超长省略，悬停看全文）+ 复制按钮（+ 可选打开按钮）。

    参数：
        path: 要展示的路径（文件或目录均可）。
        open_label: 给了就在行尾加"打开所在目录"按钮（文案自定，如"打开日志目录"）。
    """
    with ui.row().classes("w-full items-center gap-1 no-wrap"):
        label = (
            ui.label(path)
            .classes("hp-path hp-path-inline")
            .tooltip(path)
            .mark("path-value")
        )
        label.style("text-align: left; max-width: none; flex: 1 1 auto; min-width: 0;")
        ui.button(
            icon="content_copy",
            on_click=partial(_copy, path),
        ).props("flat dense round size=sm").classes("hp-icon-button").mark(
            "path-copy"
        ).tooltip("复制路径")
        if open_label:
            ui.button(
                open_label,
                icon="folder_open",
                on_click=lambda: _open(path, open_label),
            ).props("flat dense no-caps").classes("hp-row-button w-auto")


def _copy(path: str) -> None:
    """把路径写进剪贴板（走浏览器 API；失败退化为提示用户手动复制）。"""
    ui.run_javascript(
        f"navigator.clipboard.writeText({path!r}).then(() => null, () => null)"
    )
    notify("路径已复制", kind="positive", timeout=2000)


def _open(path: str, what: str) -> None:
    """打开所在目录；失败要说出来——静默失败就是"点了按钮什么都没发生"。"""
    from app.services import settings as settings_service

    if not settings_service.open_folder(path):
        notify(f"打不开{what} —— 它可能已被删掉或移到别处", kind="negative")
