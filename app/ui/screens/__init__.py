"""功能注册表：加一个功能 = 加一个页面文件 + 在这里登记一行。

为什么要有这张表：外壳（`app/shell.py`）只遍历它生成左导航与页面容器，页面自己不需要知道
被谁挂上去；这样加功能不用改外壳，也不会出现"页面写了但导航里没有"。

两种位置：

- **主列表**（`MAIN_FEATURES`）：左栏中部的功能导航（聊天…）；
- **钉在底部**（`PINNED_FEATURES`，`pinned=True`）：设置这类"随时要进、但不属于日常流程"的入口。

`build` 一律写成"函数里再 import"：页面会用到外壳的句柄（`app/state/layout`），
在这里顶层 import 页面就会形成 `shell → screens → shell` 的循环。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    """一个功能：左导航的一项 + 中栏的一页。"""

    key: str
    """稳定标识（界面态里用它，不用中文标题）。"""

    title: str
    """导航文字。"""

    icon: str
    """Material 图标名。"""

    build: Callable[[], None]
    """往"当前容器"里搭页面（由外壳在切页时调用）。"""

    pinned: bool = False
    """True = 钉在左栏底部（设置这类入口），而不是排在中部的功能列表里。"""


def _chat() -> None:
    from app.ui.screens.chat import build

    build()


def _settings() -> None:
    from app.ui.screens.settings import build

    build()


#: 全部功能（每个都是一个页面）。顺序：先中部列表，再底部钉住的。
FEATURES: tuple[Feature, ...] = (
    Feature(key="chat", title="聊天", icon="forum", build=_chat),
    Feature(key="settings", title="设置", icon="settings", build=_settings, pinned=True),
)

#: 左栏中部显示的功能
MAIN_FEATURES: tuple[Feature, ...] = tuple(f for f in FEATURES if not f.pinned)

#: 左栏底部钉住的入口
PINNED_FEATURES: tuple[Feature, ...] = tuple(f for f in FEATURES if f.pinned)
