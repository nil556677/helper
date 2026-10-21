"""设置页的卡片小件：统一的**卡片头**与**数字格**。

**为什么放项目公共层**：设置页的「通用 / 向量 / 知识库」多个分区都要用，属于"同层多处用"，
按《开发规则》R4 提到 `components/`，而不是各页面各写一份（曾经有 3 份 `_card_head`）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui


def card_head(
    icon: str,
    title: str,
    extra: str | Callable[[], Any] | None = None,
) -> None:
    """卡片头：图标 + 标题（+ 可选的状态/说明/控件组）。

    参数：
        icon: Material 图标名（如 ``"insights"`` / ``"library_books"``）。
        title: 卡片标题。
        extra: 右侧附加内容。字符串渲染为说明文字；传 **可调用对象** 则当 slot 用
            （在里面 ``ui.button(...)/ui.switch(...)`` 随意）——任务列表卡头这类
            "图标 + 标题 + 计数 + 控件组"的需求就不用再手写一遍卡头了。
    """
    with ui.row().classes("hp-card-head w-full"):
        ui.icon(icon).classes("text-base hp-card-icon")
        ui.label(title).classes("hp-card-title")
        if extra:
            ui.space()
            if callable(extra):
                extra()
            else:
                ui.label(str(extra)).classes("hp-field-help")


def stat(value: str, name: str, *, tooltip: str = "") -> None:
    """一张卡里的一格数字（浅底小方块；容器用 `.hp-stats` 自动排成网格）。

    参数：
        value: 数字/短值。
        name: 这格是什么（显示在数字下面的小字）。
        tooltip: 可选的悬停解释——"RRF k"这类术语给一句话说明，别让用户去猜。
    """
    tile = ui.column().classes("hp-stat-tile gap-0")
    if tooltip:
        tile.tooltip(tooltip)
    with tile:
        ui.label(value).classes("v")
        ui.label(name).classes("n")
