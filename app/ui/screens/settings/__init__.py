"""设置（**独立页面：自己的左栏 + 左栏顶部「返回聊天」**）：分区注册表 + 内容区外壳。

**一个分区一个文件**（用户要求；也照 DSH 的做法——`ui-settings-general` 是设置外壳，
`ui-settings-models` 等每分区一个包）：

    settings/
    ├─ __init__.py   分区清单 SECTIONS + build()（外壳用的接缝）
    ├─ general.py    通用（日志 / 调试 / 本机服务 / 关于）
    ├─ data.py       数据与存储（数据目录备份 + 向量子区）
    ├─ models.py     模型配置
    ├─ terms.py      领域词（全局自定义关键词）
    ├─ knowledge.py  知识库
    └─ tasks.py      任务

**加一个分区 = 新写一个文件 + 这里加一行**，不碰别的分区，也不碰外壳。

**接缝**：本模块只建**内容区**，并把"切分区"的函数交回外壳：``select = settings.build()``。
"左栏长什么样"只在外壳（`app/shell.py`）里定义一处，页面不重复实现导航。

**为什么不做弹框**（实测过弹框版）：弹框天生带过渡动画，而我们为了修白板把 GPU 关了、
走软件渲染（见 app/__main__.py），大卡片每帧整块重绘必然掉帧；而且弹框尺寸写死。
"""

from __future__ import annotations

from collections.abc import Callable

from nicegui import ui

from app.ui.screens.settings.approvals import render as render_approvals
from app.ui.screens.settings.data import render as render_data
from app.ui.screens.settings.general import render as render_general
from app.ui.screens.settings.knowledge import render as render_knowledge
from app.ui.screens.settings.models import render as render_models
from app.ui.screens.settings.tasks import render as render_tasks
from app.ui.screens.settings.terms import render as render_terms
from app.ui.screens.settings.traces import render as render_traces
from app.ui.state import section  # 当前分区（隐藏分区的轮询据此让路）

#: (分区 key, 标题, 图标)。顺序就是左栏里的顺序：模型配置排第二，因为不配它什么都跑不起来。
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("knowledge", "知识库", "library_books"),
    ("terms", "领域词", "sell"),
    ("tasks", "任务", "pending_actions"),
    ("traces", "检索追踪", "query_stats"),
    ("approvals", "审批", "verified_user"),
    ("models", "模型配置", "smart_toy"),
    ("data", "数据与存储", "folder_open"),
    ("general", "通用", "tune"),
)

#: 分区 key → 渲染函数。加分区就加一行。
RENDERERS: dict[str, Callable[[], None]] = {
    "general": render_general,
    "data": render_data,
    "models": render_models,
    "knowledge": render_knowledge,
    "terms": render_terms,
    "tasks": render_tasks,
    "traces": render_traces,
    "approvals": render_approvals,
}


def build() -> Callable[[str], None]:
    """搭出设置页的内容区（各分区面板），返回"切分区"的函数。

    返回：
        ``select(key)``：切到某个分区（只切可见性，零动画）。
    """
    panels: dict[str, ui.column] = {}

    # **顺序要紧**：滚动区必须建在外层 column **里面**。曾经把它建在外面（成了兄弟节点），
    # 结果那个空 column 带 h-full 把高度吃光，滚动区被挤成 0 高——用户看到的就是白板。
    with ui.column().classes("w-full h-full gap-0 hp-settings-body"):
        scroll = ui.scroll_area().classes("w-full flex-1 min-h-0 hp-settings-scroll")
        # gap/pt/pb 收紧：设置页整体去空白（标题/边框/间距是空白的主要来源）
        with scroll, ui.column().classes("w-full hp-page gap-3 pt-2 pb-6"):
            for key, _title, _icon in SECTIONS:
                panel = ui.column().classes("w-full gap-3")
                panels[key] = panel
                with panel:
                    RENDERERS[key]()

    def select(key: str) -> None:
        section.set_active(key)  # 记下当前分区（隐藏分区的轮询据此让路）
        for panel_key, panel in panels.items():
            panel.set_visibility(panel_key == key)
        # 切分区时回到顶部：两个分区高度差很大，不回顶会让用户"看到一片空白"
        # 以为点不动了（DOM 探针当初就是这么误判的）
        scroll.scroll_to(percent=0.0)

    select(SECTIONS[0][0])
    return select
