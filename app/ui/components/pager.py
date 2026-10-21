"""分页控件（任务页与知识库页共用）。

**为什么分页要服务端做**：资料可能有几千份、任务历史可能上万条，"全查出来再切页"又慢又占内存。
所以页码只决定 SQL 的 `LIMIT/OFFSET`，总数用一条 `count(*)`。

**为什么用 `ui.pagination`**：分页控件是框架自带的能力（Quasar 的 QPagination：页码、首末页、
省略号都有），自己拿按钮拼一个既丑又容易在边界上出错。我们只补两件它没有的事：
**每页条数**与**"共 N 条 · 第 x/y 页"这句话**。

用法：

```python
state = PageState(size=20)
def refresh() -> None:
    data = service.page(state.page, state.size)
    state.total = data["total"]
    state.clamp()            # 删到最后一页空了要收回来
    render_pager(state, on_change=refresh)
```
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from math import ceil

from nicegui import ui

#: 可选的每页条数（20 是默认：一屏刚好，不用滚；500 给"上万条历史想快速翻完"的场景）
PAGE_SIZES: tuple[int, ...] = (20, 50, 100, 500)


@dataclass
class PageState:
    """分页状态（页面持有它，跨刷新保留）。

    参数：
        page: 当前页（1 基）。
        size: 每页条数。
        total: 总条数（每次刷新由服务层写回）。
    """

    page: int = 1
    size: int = PAGE_SIZES[0]
    total: int = 0
    _dirty: bool = field(default=False, init=False)

    @property
    def pages(self) -> int:
        """总页数（至少 1，空列表也显示"第 1/1 页"）。"""
        return max(1, ceil(self.total / max(1, self.size)))

    @property
    def offset(self) -> int:
        """SQL OFFSET。"""
        return (self.page - 1) * self.size

    def clamp(self) -> None:
        """把页码收进合法范围（删到最后一页变空、或总数变小都要用）。"""
        self.page = min(max(1, self.page), self.pages)

    def reset(self) -> None:
        """回到第一页（换筛选条件、换每页条数时调）。"""
        self.page = 1


def render_pager(state: PageState, *, on_change: Callable[[], None], noun: str = "条") -> None:
    """画一行分页页脚（**列表卡的最下面**，与成熟后台一致）。

    参数：
        state: 分页状态（页码改动会写回它）。
        on_change: 页码或每页条数变化时调用（页面在回调里重新查库）。
        noun: 计数单位（"条"/"份"）。

    文案给的是**区间**而不是页码：`137 条 · 显示 21–40`。用户在长列表里真正想知道的是
    "我在数据的哪一段、这段有多少条"，"第 2/7 页"回答不了这个。
    """
    def go(page: int) -> None:
        state.page = page
        state.clamp()  # 边界规则只在 PageState.clamp() 一处（不要这里再写一份 min/max）
        on_change()

    first = 0 if not state.total else state.offset + 1
    last = min(state.offset + state.size, state.total)
    summary = f"{state.total} {noun}" + (f" · 显示 {first}–{last}" if state.total else "")

    with ui.row().classes("hp-pager w-full items-center gap-3 no-wrap"):
        ui.label(summary).classes("hp-field-help").mark("page-summary")
        ui.space()
        # **永远渲染**分页控件（只有 1 页时 Quasar 自己灰化）：控件出现/消失会让页脚高度
        # 跳动，下方内容跟着跳——稳定比"省一个控件"重要。
        ui.pagination(
            min=1,
            max=state.pages,
            value=state.page,
            direction_links=True,
            on_change=lambda event: go(int(event.value)),
        ).props("gutter=sm max-pages=7 boundary-numbers").classes("hp-pagination")

        def change_size(event: object) -> None:
            size = int(getattr(event, "value", PAGE_SIZES[0]) or PAGE_SIZES[0])
            if size != state.size:
                state.size = size
                state.reset()  # 换每页条数等于换了一套页，从第一页看起
                on_change()

        # borderless：页脚里视觉最重的控件不该是最不重要的那个（"每页"二字交给 summary 语境）
        ui.select(
            {size: str(size) for size in PAGE_SIZES},
            value=state.size,
            on_change=change_size,
        ).props("borderless dense options-dense").classes("hp-page-size").tooltip(
            "每页条数"
        )
