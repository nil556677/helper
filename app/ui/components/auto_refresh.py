"""自动刷新控件（列表页共用）：开关 + "刚刚更新" + 手动刷新。

**为什么必须有它**（成熟列表页的标配：Sidekiq Web 的 Poll 开关、Flower、Bull Board 都有）：
列表在**自己动**的时候，如果不告诉用户，用户会怀疑"是我点错了吗 / 数据怎么变了"；
而想对照着看某几行时又没法让它停下来。所以三件事缺一不可：

1. **开关**：能关；关掉之后列表就冻结，用户看到的数字不会再变；
2. **时间戳**："刚刚更新 / 12 秒前更新"——一眼知道眼前这份数据有多新；
3. **手动刷新按钮**：关掉自动刷新之后，仍能主动拉一次。

只有第一件事是"功能"，后两件是"信任"——省掉它们，开关本身也会让人不安。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nicegui import ui


@dataclass
class AutoRefresh:
    """页面持有的刷新状态（跨刷新保留）。

    参数：
        interval_s: 轮询间隔（秒）。任务进度写库有 0.2 秒节流，所以 0.5 秒够跟得上。
        enabled: 是否开着自动刷新。
        label: 里面会挂上"刚刚更新"那个标签（`render()` 时创建）。

    用法：

    ```python
    auto = AutoRefresh(interval_s=0.5)
    ...
    auto.render(refresh)          # 放在工具栏右侧
    def refresh(...) -> None:
        ...                       # 查库、重画
        auto.touch()              # 刷新成功后记账（时间戳 + 启动轮询）
    ```
    """

    interval_s: float = 0.5
    enabled: bool = True
    label: Any = None
    _last: float = field(default=0.0, init=False)
    _timer: Any = field(default=None, init=False)

    def text(self) -> str:
        """给用户看的一句话（关掉时也要说清"数据是什么时候的"）。"""
        if not self._last:
            return "还没刷新"
        seconds = max(0.0, time.time() - self._last)
        if seconds < 3:
            when = "刚刚更新"
        elif seconds < 60:
            when = f"{int(seconds)} 秒前更新"
        elif seconds < 3600:
            when = f"{int(seconds // 60)} 分钟前更新"
        else:
            when = f"{int(seconds // 3600)} 小时前更新"
        return when if self.enabled else f"自动刷新已关 · {when}"

    def touch(self) -> None:
        """记一次"刚刷新过"（刷新成功后调用）。"""
        self._last = time.time()
        self.refresh_label()

    def refresh_label(self) -> None:
        """把时间戳标签更新成当前文案（每秒也要调一次，否则"12 秒前"会停在"刚刚"）。"""
        if self.label is not None:
            self.label.text = self.text()
            self.label.update()

    def render(self, on_refresh: Callable[[], None]) -> None:
        """画「自动刷新」开关 + 时间戳 + 手动刷新按钮，并启动轮询定时器。

        参数：
            on_refresh: 手动刷新（也是轮询要调的那个函数）。
        """
        def toggle(event: Any) -> None:
            self.enabled = bool(getattr(event, "value", True))
            self.refresh_label()

        ui.switch("自动刷新", value=self.enabled, on_change=toggle).props("dense").classes(
            "hp-auto-switch"
        ).mark("auto-refresh")
        self.label = ui.label("").classes("hp-field-help").mark("auto-stamp")
        ui.button(icon="refresh", on_click=lambda: on_refresh()).props("flat dense round size=sm").mark(
            "manual-refresh"
        ).tooltip("立即刷新")

        def tick() -> None:
            if self.enabled:
                on_refresh()
            else:
                self.refresh_label()  # 关掉之后时间戳仍要走字，否则用户不知道数据多旧了

        self._timer = ui.timer(self.interval_s, tick)
