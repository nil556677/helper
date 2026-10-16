"""统一的界面弹窗层（helper 风格）。

**为什么要有它**：以前确认框、信息框、错误框、通知条是各处手写的——
``knowledge.py`` / ``tasks.py`` 各自 ``with ui.dialog()+ui.card()`` 拼一套，
``ui.notify`` 的 positive/negative/info 颜色也散落各处。用户要求的"所有类型弹窗统一"，
就是让四类（确认 / 信息 / 错误 / 成功）观感一致、一句话可调。

**约定**：
- 组件只认我们的样式类（``hp-*``），不写死的白底弹窗用它自己的卡片；
- 所有弹窗走同一个壳（标题 + 图标 + 正文 + 底部动作），区别只在语义色与按钮；
- ``on_confirm`` 由调用方传入（确认后要做的事，闭包里的状态照样能访问），
  这里只负责"建框 + 关框"，不决定业务。

**注意**：NiceGUI 的 ``ui.dialog`` 是异步 open 的；按钮回调里先执行业务再 ``close()``。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

#: 语义色 → 图标与强调类的映射（错误红 / 成功绿 / 警示橙 / 信息蓝）。
_ICONS: dict[str, str] = {
    "confirm": "help_outline",
    "info": "info_outline",
    "success": "check_circle_outline",
    "error": "error_outline",
    "warning": "warning_amber",
}

#: 语义色 → 图标颜色类（跟 theme.py 的状态点色对齐：#c0392b 红 / #2f9e62 绿）。
_ICON_COLOR: dict[str, str] = {
    "confirm": "var(--hp-label-tertiary)",
    "info": "var(--hp-accent)",
    "success": "#2f9e62",
    "error": "#c0392b",
    "warning": "#e6a23c",
}

#: 默认标题。
_TITLES: dict[str, str] = {
    "confirm": "确认",
    "info": "提示",
    "success": "完成",
    "error": "出错了",
    "warning": "注意",
}


def _dialog_body(kind: str, title: str, message: str) -> None:
    """弹窗主体：标题行（图标 + 标题）+ 正文。"""
    with ui.row().classes("w-full items-center gap-2 no-wrap"):
        ui.icon(_ICONS[kind]).style(f"color: {_ICON_COLOR[kind]}; font-size: 22px;")
        ui.label(title).classes("hp-card-title")
    ui.label(message).classes("hp-field-help").style("white-space: pre-wrap; word-break: break-word;")
    ui.separator()


def show_dialog(
    message: str,
    *,
    kind: str = "info",
    title: str | None = None,
    confirm_label: str = "知道了",
    on_confirm: Callable[[], Any] | None = None,
) -> None:
    """弹一个信息框（错误 / 成功 / 提示共用，差异只在语义色与标题）。

    参数：
        message: 正文（可多行，会保留换行）。
        kind: ``info`` / ``success`` / ``error`` / ``warning`` / ``confirm``。
        title: 标题；默认按 ``kind``。
        confirm_label: 底部按钮文字。
        on_confirm: 点按钮后执行（**缺省只关框**）。
    """
    resolved = title or _TITLES.get(kind, "提示")
    with ui.dialog() as dialog, ui.card().classes("hp-card hp-dialog gap-3"):
        _dialog_body(kind, resolved, message)
        with ui.row().classes("hp-actions w-full justify-end"):
            ui.button(
                confirm_label,
                on_click=lambda: (
                    on_confirm() if on_confirm else None,
                    dialog.close(),
                ),
            ).props("unelevated dense no-caps").classes("hp-send hp-dialog-ok")
    dialog.open()


def confirm(
    title: str,
    message: str,
    on_confirm: Callable[[], Any],
    *,
    confirm_label: str = "确定",
    cancel_label: str = "取消",
    danger: bool = False,
    confirm_mark: str | None = None,
) -> None:
    """需确认的弹窗（破坏性操作用）：取消 / 确认两个按钮。

    确认按钮默认主色；``danger=True`` 时换成红（删数据这类，明确"这是危险的"）。

    参数：
        title: 标题。
        message: 正文（说明后果，给用户可预期的下一步）。
        on_confirm: 点"确定"后要做的动作（闭包里原有状态照常可访问）。
        confirm_label: 确认按钮文字（如"删掉"）。
        cancel_label: 取消按钮文字。
        danger: 确认按钮是否用危险红。
        confirm_mark: 确认按钮的界面测试锚点（``.mark(...)``）。
    """
    with ui.dialog() as dialog, ui.card().classes("hp-card hp-dialog gap-3"):
        _dialog_body("confirm", title, message)
        with ui.row().classes("hp-actions w-full justify-end"):
            ui.button(
                cancel_label,
                on_click=dialog.close,
            ).props("flat dense no-caps").classes("hp-row-button w-auto")
            button = ui.button(
                confirm_label,
                on_click=lambda: (
                    on_confirm(),
                    dialog.close(),
                ),
            ).props("unelevated dense no-caps").classes(
                "hp-row-button hp-confirm-danger w-auto" if danger else "hp-send hp-dialog-ok w-auto"
            )
            if confirm_mark:
                button.mark(confirm_mark)
    dialog.open()


def notify(
    message: str,
    *,
    kind: str = "info",
    closeable: bool = True,
    actions: list[dict[str, Any]] | None = None,
    timeout: int | None = None,
) -> None:
    """统一的通知条（toast）：把视觉统一，但仍可透传带动作的 toast。

    参数：
        message: 文案。
        kind: ``positive``（成功）/ ``negative``（错误）/ ``warning`` / ``info``。
        closeable: 可否手动关。
        actions: Quasar notify 的 ``actions``（形如 ``[{"label": "撤销", "handler": fn}]``），
            给"需要用户回应的操作"（如撤销取消）用。有 action 时默认不自动消失。
        timeout: 自动消失毫秒数；None 时按 kind 给默认（positive 短暂、其余停留）。
    """
    ui.notify(
        message,
        type=kind,
        close_button="关闭" if closeable else None,
        actions=actions,
        timeout=timeout if timeout is not None else (3500 if kind == "positive" else None),
        position="top-right",
        classes="hp-toast",
    )


def custom(
    *,
    kind: str = "info",
    title: str | None = None,
) -> tuple[ui.dialog, Callable[[], None]]:
    """复杂内容的弹窗（任务详情这类：正文不是一句 message，而是时间线/列表）。

    用法：

        dialog, close = custom(title="任务详情", kind="info")
        with dialog:
            … 正文 …
            ui.button("关闭", on_click=close).props("flat dense no-caps").classes(...)
        dialog.open()

    工厂只负责统一壳（``hp-dialog``）+ 标题行（图标 + 标题 + 分隔线）+ ``close()`` 动作；
    正文与底部动作由调用方在 ``with dialog`` 里填，沿用统一壳。
    """
    resolved = title or _TITLES.get(kind, "提示")
    with ui.dialog() as dialog, ui.card().classes("hp-card hp-dialog gap-3"):
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            ui.icon(_ICONS[kind]).style(f"color: {_ICON_COLOR[kind]}; font-size: 22px;")
            ui.label(resolved).classes("hp-card-title")
        ui.separator()
    return dialog, dialog.close


def show_error(message: str, *, title: str | None = None) -> None:
    """错误弹窗（红）：等用户看到再关。"""
    show_dialog(message, kind="error", title=title, confirm_label="知道了")


def show_success(message: str, *, title: str | None = None) -> None:
    """成功弹窗（绿）。"""
    show_dialog(message, kind="success", title=title, confirm_label="知道了")
