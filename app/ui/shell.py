"""外壳：两种模式 + 左栏 / 中栏 / 右栏（都是抽屉）。

**两种模式**（设置是一个完整独立页面）：

- **聊天模式**：左栏 = 品牌 + 功能导航 + 底部钉住的入口；中栏 = 当前功能页；
- **设置模式**：左栏顶部 = 「返回聊天」，下面是设置分区（通用 / 数据 / 关于）；中栏只留标题；
  **聊天模式的导航与内容在这个模式下全部隐藏**。

切换模式只是切可见性（零动画）——不弹框、不重建元素：弹框天生带过渡动画，
而我们把 GPU 关了走软件渲染（见 app/__main__.py），大面积动画必然掉帧。

几何照 DSH 的 Web GUI 布局规格（`dsh-client-ui-layout/README.zh.md`）：

- 左栏 264–420px、默认 280px，收起保留 56px 控制栏；窗口宽度 < 1024px 自动收起；
- 右栏是"贴边面板的轨道"：首次打开用视口宽 45%、上限 70%，中栏至少留 400px。

右栏宽度现在是按窗口宽度算好写死的；随窗口尺寸动态钳制那套规则等真正的面板管理落地时再补。
"""

from __future__ import annotations

from functools import partial
from typing import Any

from loguru import logger
from nicegui import ui

from app import tasks as tasks_app
from app.ui.resources.theme import apply as apply_theme
from app.ui.screens import FEATURES, MAIN_FEATURES, PINNED_FEATURES
from app.ui.screens import settings as settings_screen
from app.services import tasks as tasks_service
from app.ui.state import conversations
from app.ui.state.layout import layout

# ---- 左栏（DSH：264~420px，默认 280px，收起保留 56px，<1024px 自动收起）----
SIDEBAR_WIDTH = 280
SIDEBAR_MINI_WIDTH = 56
COLLAPSE_BREAKPOINT = 1024

# ---- 右栏（DSH：首次 45% 视口宽，上限 70%，最小 300px，中栏至少留 400px）----
RIGHTBAR_FIRST_RATIO = 0.45
RIGHTBAR_MIN_WIDTH = 300
MAIN_MIN_WIDTH = 400

#: 默认窗口尺寸：**只在第一次运行时用**，之后由用户上次的几何决定（存在 SQLite 的 settings 表）。
#: 右栏宽度按窗口宽度算，所以这里存的是"当前窗口宽度"，由入口在恢复几何之后告诉它。
WINDOW_SIZE = (1440, 900)
_window_width = WINDOW_SIZE[0]


def set_window_width(width: int) -> None:
    """告诉外壳真正的窗口宽度（入口在建好窗口、恢复完几何之后调一次）。

    为什么需要：右栏宽度按窗口宽度的 45% 算，而窗口尺寸是用户上次调过的——
    写死 1440 会在大窗口下显得太窄、在小窗口下把中栏挤到 400px 以下（§8 要求中栏至少留 400px）。
    """
    global _window_width
    if width > 0:
        _window_width = width


def _rightbar_width() -> int:
    """右栏宽度：窗口宽的 45%，但不小于 300px，并给中栏留下至少 400px。"""
    wanted = int(_window_width * RIGHTBAR_FIRST_RATIO)
    keep_for_main = _window_width - MAIN_MIN_WIDTH
    return max(RIGHTBAR_MIN_WIDTH, min(wanted, keep_for_main))


def _toggle_sidebar(drawer: object, state: dict[str, bool]) -> None:
    """在 280px 与 56px 之间切换左栏。

    DSH 的收起是**保留 56px 控制栏**，不是整列消失，所以用 Quasar 的 mini 模式，
    而不是把抽屉关掉（关掉会让中栏宽度跳动）。
    """
    state['mini'] = not state['mini']
    if state['mini']:
        drawer.props('mini')  # type: ignore[attr-defined]
    else:
        drawer.props(remove='mini')  # type: ignore[attr-defined]


def build_shell() -> None:
    """搭出两种模式的骨架（每个窗口调一次）。"""
    apply_theme()
    # 浏览器侧（页内）异常必须**自己接**：NiceGUI 的 ``Client.handle_exception`` 只遍历注册过的钩子
    # （nicegui/client.py），**没人注册就静默丢掉** —— 界面上"点了没反应、日志里什么都没有"
    # 多半就是这一类（技术方案 §10.1「界面」层）。用 opt(exception=...) 显式带上异常对象，
    # 不依赖 sys.exc_info（这里已经不在那个 except 块里了）。
    ui.on_exception(lambda exc: logger.opt(exception=exc).error("界面上抛出未处理异常"))
    # 事件循环延迟看门狗：任务系统据此在界面卡顿时给吃 CPU 的任务让路（§13.5）。
    # 在这里启动是因为**只有页面构建时才确定拿得到 NiceGUI 服务线程的 asyncio 循环**。
    tasks_app.ensure_lag_monitor()
    shell = layout()
    state: dict[str, object] = {'key': MAIN_FEATURES[0].key, 'mini': False}
    pages: dict[str, ui.column] = {}
    nav_items: dict[str, ui.button] = {}
    section_items: dict[str, ui.button] = {}
    #: 设置页把"切分区"的函数交给外壳（见 app/screens/settings.py 的说明）
    settings_api: dict[str, object] = {'select': None}

    left = (
        ui.left_drawer(value=True)
        .props(f'width={SIDEBAR_WIDTH} mini-width={SIDEBAR_MINI_WIDTH} breakpoint={COLLAPSE_BREAKPOINT}')
        .classes('p-0 hp-sidebar')
    )
    right = (
        ui.right_drawer(value=False)
        .props(f'width={_rightbar_width()}')
        .classes('p-0 hp-panel')
    )
    with right:
        right_panel = ui.column().classes('w-full gap-0')

    shell.right = right
    shell.right_panel = right_panel

    # ---- 切换逻辑（先定义：下面的按钮要引用它们；它们引用到的容器稍后才建，
    #      但闭包在"被调用时"才解析名字，所以顺序没问题） ----
    def select_section(key: str) -> None:
        """切设置分区：内容交给设置页，高亮由外壳负责。"""
        selector = settings_api['select']
        if callable(selector):
            selector(key)
        for item_key, button in section_items.items():
            button.classes(add='hp-row-active') if item_key == key else button.classes(
                remove='hp-row-active'
            )

    def open_page(key: str) -> None:
        """切到聊天模式的某一页（左栏/顶栏/内容整套切回聊天）。"""
        if key not in pages:
            return
        state['key'] = key
        chat_side.set_visibility(True)
        settings_side.set_visibility(False)
        chat_top.set_visibility(True)
        settings_top.set_visibility(False)
        chat_box.set_visibility(True)
        settings_box.set_visibility(False)
        for page_key, box in pages.items():
            box.set_visibility(page_key == key)
        for item_key, button in nav_items.items():
            button.classes(add='hp-row-active') if item_key == key else button.classes(
                remove='hp-row-active'
            )
        shell.title.text = next(f.title for f in FEATURES if f.key == key)
        shell.title.update()

    def open_settings_page() -> None:
        """切到设置模式：聊天模式的导航与内容**全部隐藏**。"""
        # 右栏是"看某条命中片段"的贴边面板，进设置时它属于上一个页面，必须收起来
        # （否则设置页右边还挂着一条资料片段）。
        shell.close_right()
        chat_side.set_visibility(False)
        settings_side.set_visibility(True)
        chat_top.set_visibility(False)
        settings_top.set_visibility(True)
        chat_box.set_visibility(False)
        settings_box.set_visibility(True)

    def refresh_conversations() -> None:
        """重画左栏的会话列表（新建 / 自动命名 / 追加了一轮都会触发）。"""
        items = conversations.conversations()
        conversation_box.clear()
        if not items:
            # 还没种子会话时不要调 current()——它会顺手建一个空会话，把种子会话挤掉
            return
        active_id = conversations.current().id
        with conversation_box:
            with ui.column().classes('w-full gap-0'):
                for conversation in items:
                    # 同样用 button（ui.item 的点击在真机不触发，见上面功能导航的注释）
                    classes = 'w-full hp-row-button hp-conv-row'
                    if conversation.id == active_id:
                        classes += ' hp-row-active'
                    row = ui.button(conversation.title).props('flat dense no-caps align=left').classes(
                        classes
                    ).mark(f'conv-{conversation.id}')
                    row.on('click', partial(open_conversation, conversation.id))

    def open_conversation(conversation_id: str) -> None:
        """切到某个会话：聊天页会把对话流整块重画。"""
        conversations.select(conversation_id)
        open_page(MAIN_FEATURES[0].key)  # 会话列表只在聊天模式里，切会话即回聊天

    def new_conversation() -> None:
        """新建一个空会话并切过去。"""
        conversations.new()
        open_page(MAIN_FEATURES[0].key)

    conversations.subscribe_list(refresh_conversations)

    shell.open_page = open_page

    # ---- 左栏：两套内容，同一时刻只显示一套（隐藏的那套不占位）----
    with left:
        with ui.column().classes('w-full h-full justify-between p-3 gap-2') as chat_side:
            # 聊天模式：品牌 / 新建对话 / 会话列表 / 底部钉住的入口
            with ui.column().classes('w-full flex-1 min-h-0 gap-2'):
                with ui.row().classes('items-center gap-2 px-1'):
                    ui.icon('hub').classes('text-lg')
                    ui.label('helper').classes('hp-brand text-sm')
                ui.button('新建对话', icon='add', on_click=new_conversation).props(
                    'flat dense no-caps align=left'
                ).classes('w-full hp-row-button').mark('new-chat')

                # 只有一个功能时，功能导航不占地方（左栏这块位置留给会话列表）；
                # 以后功能多了（>1）再显示出来。
                if len(MAIN_FEATURES) > 1:
                    ui.separator()
                    with ui.column().classes('w-full gap-0'):
                        for feature in MAIN_FEATURES:
                            # **用 button 而不是 ui.item**：实测 `ui.item` 的点击在真实浏览器里
                            # 不触发（`item.on('click', …)` 在模拟器里能过，是因为模拟器直接调回调、
                            # 不走 DOM；真机点了没反应，日志里也没有任何异常）。button 到处都好使。
                            nav_button = (
                                ui.button(feature.title, icon=feature.icon)
                                .props('flat dense no-caps align=left')
                                .classes('w-full hp-row-button')
                                .mark(f'nav-{feature.key}')
                            )
                            nav_button.on('click', partial(open_page, feature.key))
                            nav_items[feature.key] = nav_button

                ui.separator()
                conversation_box = ui.column().classes(
                    'w-full flex-1 min-h-0 gap-0 overflow-auto'
                )

            with ui.column().classes('w-full gap-1'):
                ui.separator()
                # 钉在底部的入口（设置）：进的是一个完整独立页面，不是弹框
                for feature in PINNED_FEATURES:
                    with ui.row().classes('w-full items-center gap-1 no-wrap'):
                        ui.button(
                            feature.title, icon=feature.icon, on_click=open_settings_page
                        ).props('flat dense no-caps align=left').classes(
                            'w-full hp-row-button'
                        ).mark(f'pin-{feature.key}')
                        # 有活跃任务时显示条数：不用打开设置也知道"跑完了没"
                        badge = ui.label('').classes('hp-badge').mark('task-badge')

                    def update_badge(label: Any = badge) -> None:
                        count = tasks_service.active_count()
                        text = str(count) if count else ''
                        if label.text != text:  # 没变就别动 DOM（避免无谓重绘）
                            label.text = text
                            label.update()

                    ui.timer(2.0, update_badge)

        with ui.column().classes('w-full h-full p-3 gap-2') as settings_side:
            # 设置模式：自己的左栏。**「返回聊天」放在左栏顶部**（用户定的）：
            # 它是"离开这一页"的动作，和导航属于同一类，放一起视线不用来回跳。
            with ui.column().classes('w-full gap-2'):
                ui.button(
                    '返回聊天', icon='arrow_back', on_click=partial(open_page, MAIN_FEATURES[0].key)
                ).props('flat dense no-caps align=left').classes('w-full hp-row-button').mark(
                    'back-to-chat'
                )
                ui.separator()
                with ui.column().classes('w-full gap-0'):
                    for key, title, icon in settings_screen.SECTIONS:
                        # 用 button 而不是 ui.item（真机上 ui.item 的点击不触发，见上）
                        button = (
                            ui.button(title, icon=icon)
                            .props('flat dense no-caps align=left')
                            .classes('w-full hp-row-button')
                            .mark(f'section-{key}')
                        )
                        button.on('click', partial(select_section, key))
                        section_items[key] = button

    # ---- 中栏：聊天模式的顶栏 / 设置模式的顶栏（含「返回聊天」）+ 两套内容 ----
    # **高度必须"确定"**：整页 `.q-layout`/`.q-page`/`.nicegui-content` 的高度实测都是 0，
    # 全靠内容自然高度撑；两套内容又都是 flex-1，互相依赖就会一起塌成 0，滚动区被裁掉 → 白板。
    # 所以这里用 `.hp-main`（100vh，见 theme.py 第 11 节），别用 Tailwind 的 h-screen（那份构建里没有）。
    with ui.column().classes('w-full hp-main'):
        with ui.row().classes('w-full items-center gap-2 px-3 py-2 hp-topbar') as chat_top:
            ui.button(icon='menu', on_click=partial(_toggle_sidebar, left, state)).props(
                'flat dense round'
            ).classes('hp-icon-button')
            shell.title = ui.label(MAIN_FEATURES[0].title).classes('hp-topbar-title text-sm')

        with ui.row().classes('w-full items-center gap-2 px-3 py-2 hp-topbar') as settings_top:
            # 「返回聊天」在左栏顶部（见上）；这里只留标题，好和聊天模式顶栏等高，切模式不跳
            ui.label('设置').classes('hp-topbar-title text-sm')

        with ui.column().classes('w-full hp-main-page') as chat_box:
            for feature in MAIN_FEATURES:
                box = ui.column().classes('w-full h-full')
                pages[feature.key] = box
                with box:
                    feature.build()

        with ui.column().classes('w-full hp-main-page hp-settings-box') as settings_box:
            settings_api['select'] = settings_screen.build()

    # 起始状态：聊天模式；另一套全部隐藏
    settings_side.set_visibility(False)
    settings_top.set_visibility(False)
    settings_box.set_visibility(False)
    # 会话列表要先画一次**初始状态**：它平时靠"有变化"的通知重画，
    # 而重新加载 / 重连时（新客户端）是没有变化通知的——不画就空着
    # （DOM 探针就是这么发现的：HTTP 预检建了一个客户端，WebView 又建了一个）
    refresh_conversations()
    open_page(str(state['key']))
