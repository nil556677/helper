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
        if self.right is not None:
            self.right.value = True

    def close_right(self) -> None:
        """收起右栏（中栏立刻拿回空间）。"""
        if self.right is not None:
            self.right.value = False

    def show_right(self, build_content: Any) -> None:
        """清空右栏 → 用 ``build_content`` 重新填 → 展开。

        参数：
            build_content: 一个无参可调用对象，内部用 ``ui.*`` 建内容（在右栏上下文里执行）。
        """
        if self.right_panel is None:
            return
        self.right_panel.clear()
        with self.right_panel:
            build_content()
        self.open_right()


_current = Layout()


def layout() -> Layout:
    """取当前窗口的外壳句柄。"""
    return _current
