"""外壳各栏的句柄（页面用它开关右栏、切页）。

为什么独立成模块：外壳（`app.ui.shell`）要 import 页面注册表拿到页面列表，而页面又需要外壳的
右栏句柄——放在这里，两边都只依赖它，不会形成循环 import。

**单窗口假设**：本项目是单机单窗口桌面应用，所以用模块级单例。将来若要支持多窗口/多客户端，
把它换成 NiceGUI 的 client 级存储即可（调用方的接口不用变）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Layout:
    """一扇窗口的界面外壳句柄（各字段由 `app.ui.shell` 在搭骨架时填上）。"""

    #: 右栏（贴边面板）
    right: Any = None
    #: 右栏里的内容容器：每次打开前先清空
    right_panel: Any = None
    #: 中栏标题（切页时改文字）
    title: Any = None

    def open_right(self) -> None:
        """展开右栏。"""
        # 路径 1：外壳还没搭好（right 为 None）→ 静默忽略，避免启动期抛 AttributeError。
        if self.right is not None:
            # 路径 2：正常展开（NiceGUI 的 right drawer 用 .value 控制开关）。
            self.right.value = True

    def close_right(self) -> None:
        """收起右栏（中栏立刻拿回空间）。"""
        # 路径 1：外壳还没搭好 → 忽略，保持与 open_right 对称。
        if self.right is not None:
            # 路径 2：正常收起。
            self.right.value = False

    def show_right(self, build_content: Any, *, title: str = "预览") -> None:
        """清空右栏 → 画**头行（标题 + 关闭 ×）** + 可滚动内容 → 展开。

        参数：
            build_content: 一个无参可调用对象，内部用 ``ui.*`` 建内容（在右栏上下文里执行）。
            title: 头行标题（默认「预览」）。
        """
        # 路径 1：右栏内容容器不存在（外壳没搭好/已销毁）→ 直接返回，不抛错。
        if self.right_panel is None:
            return
        # 延迟导入：模块级导入 ui 会让 layout 强依赖 NiceGUI，测试/非 UI 场景不好用。
        from nicegui import ui

        # 每次打开先清空，避免上一次的内容残留（右栏是复用同一个容器）。
        self.right_panel.clear()
        with self.right_panel:
            # 头行：标题 + 关闭按钮（关闭按钮复用 close_right，行为与 Esc 一致）。
            with ui.row().classes("w-full items-center gap-2 no-wrap hp-panel-head"):
                ui.label(title).classes("hp-card-title")
                ui.space()
                ui.button(icon="close", on_click=self.close_right).props(
                    "flat dense round size=sm"
                ).classes("hp-panel-close").mark("panel-close").tooltip("关闭（Esc）")
            # 正文：交给调用方在右栏上下文里建内容（引用详情、文档预览等）。
            with ui.column().classes("w-full gap-2 hp-panel-body"):
                build_content()
        # 先填内容再展开，避免"先展开后填充"闪一下空白。
        self.open_right()


_current = Layout()


def layout() -> Layout:
    """取当前窗口的外壳句柄。"""
    return _current
