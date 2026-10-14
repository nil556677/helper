"""共用界面组件（**可复用的零件**，不是页面）。

放这里的判断标准：**两个以上页面会用到**，而且与具体业务无关。
只有一个页面用的东西留在那个页面里（`app/screens/…`），别往这里塞。

现有零件：

| 零件 | 解决什么 |
| --- | --- |
| `pager` | 服务端分页的页脚：区间文案 + 页码 + 每页条数 |
| `auto_refresh` | 列表在"自己动"时的信任问题：开关 + 刚刚更新 + 手动刷新 |
"""

from app.ui.components.auto_refresh import AutoRefresh
from app.ui.components.pager import PAGE_SIZES, PageState, render_pager

__all__ = ["PAGE_SIZES", "AutoRefresh", "PageState", "render_pager"]
