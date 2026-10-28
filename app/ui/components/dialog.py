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

import contextlib
import inspect
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from loguru import logger
from nicegui import ui

#: 每个客户端当前"活着的确认框"：``{客户端键: (本次弹窗的唯一 token, 弹窗元素)}``。
#:
#: 为什么**不是**布尔标志：早先 `_OPEN[客户端] = True` 只在弹窗的 ``hide`` 事件里清除，
#: 而该事件在本环境不可靠（实测不触发）——标志卡死，第二次点删除 `confirm()` 直接
#: return，界面"没反应"。换成 token + 元素引用后：
#:
#: - 每次弹窗调用都生成**自己的 token**，它的确认/取消/×/关闭/清除全都带着 token 走；
#: - 新弹窗来时先按旧 token **关掉旧框**再开新的——没有任何会卡死的状态；
#: - 按 ``client.id`` 分键，测试里每个用例是新客户端，不会互相污染。
_ACTIVE: dict[str, tuple[str, ui.dialog | None]] = {}


def _client_key() -> str:
    """当前客户端的键（拿不到就退回全局，功能仍可用）。"""
    try:
        from nicegui import context

        return str(context.client.id)
    except Exception:
        return "global"


@contextlib.contextmanager
def _dialog_parent() -> Iterator[None]:
    """把对话框挂到**客户端稳定根槽位**（而不是调用方的槽位）。

    **为什么必须这样**：NiceGUI 的事件回调运行在「发送元素所在的槽位」上下文里
    （``handle_event`` 里 ``with sender.parent_slot``）。如果那个槽位属于一个会被刷新的容器
    ——向量页的 ``body``、知识库详情的 ``content``/``body``——而确认回调又在其中执行了
    ``refresh()``/``back()``（它们 ``clear()`` 整块容器），那么**还开着的对话框连同它的
    按钮会被一起删掉**，回调收尾再碰已删除的元素，NiceGUI 就报
    "An element has been deleted but is still being used"（实测：清空向量集合后必现）。

    挂到根槽位后，任何子容器的 ``clear()`` 都碰不到它；对话框打开期间容器怎么刷新都安全。
    """
    from nicegui import context

    try:
        root = context.client.layout.default_slot
    except Exception:
        root = None
    if root is None:
        yield  # 拿不到根槽（极端情况）：按原行为挂到当前槽位
        return
    with root:
        yield


async def _call(maybe: Callable[[], Any] | None) -> None:
    """执行回调；回调是 async 就等它（删除这类慢活会自己丢线程池）。"""
    if maybe is None:
        return
    result = maybe()
    if inspect.isawaitable(result):
        await result


def _destroy(dialog: ui.dialog) -> None:
    """关闭并**从页面删除**弹窗。

    只 ``close()`` 的话元素还留在 DOM 里：每点一次详情/确认就多一堆节点，
    点得越多越卡（实测：反复点资料详情后明显掉帧）。删掉才是真的收尾。
    """
    with contextlib.suppress(Exception):
        dialog.close()
    with contextlib.suppress(Exception):
        dialog.delete()


def _dialog_head(
    kind: str,
    title: str,
    dialog: ui.dialog,
    *,
    on_close: Callable[[], Any] | None = None,
) -> None:
    """弹窗头：图标 + 标题 + 右上角的 ×（任何框都能随手关掉）。"""
    with ui.row().classes("w-full items-center gap-2 no-wrap"):
        ui.icon(_ICONS[kind]).style(f"color: {_ICON_COLOR[kind]}; font-size: 22px;")
        ui.label(title).classes("hp-card-title")
        ui.space()
        ui.button(
            icon="close",
            on_click=on_close or (lambda: _destroy(dialog)),
        ).props("flat dense round size=sm").classes("hp-dialog-close").mark(
            f"dialog-close-{kind}"
        ).tooltip("关闭")

#: 语义色 → 图标与强调类的映射（错误红 / 成功绿 / 警示橙 / 信息蓝）。
_ICONS: dict[str, str] = {
    "confirm": "help_outline",
    "info": "info_outline",
    "success": "check_circle_outline",
    "error": "error_outline",
    "warning": "warning_amber",
}

#: 语义色 → 图标颜色类（引用 theme.py 的语义色变量：不写死 hex，暗色模式落地时自动跟随）。
_ICON_COLOR: dict[str, str] = {
    "confirm": "var(--hp-label-tertiary)",
    "info": "var(--hp-accent)",
    "success": "var(--hp-success)",
    "error": "var(--hp-danger)",
    "warning": "var(--hp-warn)",
}

#: 默认标题。
_TITLES: dict[str, str] = {
    "confirm": "确认",
    "info": "提示",
    "success": "完成",
    "error": "出错了",
    "warning": "注意",
}


def _dialog_body(
    kind: str,
    title: str,
    message: str,
    dialog: ui.dialog,
    *,
    on_close: Callable[[], Any] | None = None,
) -> None:
    """弹窗主体：标题行（图标 + 标题 + ×）+ 正文。"""
    _dialog_head(kind, title, dialog, on_close=on_close)
    ui.label(message).classes("hp-field-help q-mb-sm").style(
        "white-space: pre-wrap; word-break: break-word;"
    )
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
    with _dialog_parent():
        with ui.dialog() as dialog, ui.card().classes("hp-card hp-dialog gap-3"):
            _dialog_body(
                kind, resolved, message, dialog, on_close=lambda: _destroy(dialog)
            )
            with ui.row().classes("hp-actions w-full justify-end"):

                async def _ok() -> None:
                    await _call(on_confirm)
                    _destroy(dialog)

                ui.button(confirm_label, on_click=_ok).props("unelevated dense no-caps").classes(
                    "hp-send hp-dialog-ok"
                ).mark("dialog-ok")
        dialog.on("hide", lambda: _destroy(dialog))
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

    **防连点**：同一时刻只允许一个确认框（连点删除不会堆出多个）；确认期间按钮转圈并禁用。

    参数：
        title: 标题。
        message: 正文（说明后果，给用户可预期的下一步）。
        on_confirm: 点"确定"后要做的动作（可以是 **async**；删除这类慢活应自己 await run.io_bound）。
        confirm_label: 确认按钮文字（如"删掉"）。
        cancel_label: 取消按钮文字。
        danger: 确认按钮是否用危险红。
        confirm_mark: 确认按钮的界面测试锚点（``.mark(...)``）。
    """
    key = _client_key()
    token = uuid.uuid4().hex  # 本次弹窗的唯一标识：它的一切操作都带着这个 token

    # 防连点：**不静默返回**（那正是"第二次点没反应"的根因）。上一次的框还开着就
    # 先在 Python 侧关掉，再开新的——新框自动替掉旧框，连点也不会堆出多个。
    # **危险框例外**：用户正在确认"删 20 条记录"这类操作时，别的框（哪怕也是确认）
    # 不得无声顶掉它——被顶掉会被误以为"确认过了"。此时新框不弹、给出提示。
    previous = _ACTIVE.get(key)
    if previous is not None:
        if danger:
            notify("先处理完当前确认框再继续", kind="info")
            return
        with contextlib.suppress(Exception):
            _destroy(previous[1])
        _ACTIVE.pop(key, None)

    def _clear() -> None:
        """清掉"当前框"——**只认自己的 token**：别把后开的框误清了。"""
        current = _ACTIVE.get(key)
        if current is not None and current[0] == token:
            _ACTIVE.pop(key, None)

    with _dialog_parent():
        with ui.dialog() as dialog, ui.card().classes("hp-card hp-dialog gap-3"):
            _dialog_body(
                "confirm", title, message, dialog, on_close=lambda: _destroy(dialog)
            )
            with ui.row().classes("hp-actions w-full justify-end"):
                cancel_button = ui.button(
                    cancel_label,
                    on_click=lambda: (_clear(), _destroy(dialog)),
                ).props("flat dense no-caps").classes("hp-row-button w-auto").mark("dialog-cancel")
                button = ui.button(
                    confirm_label,
                ).props("unelevated dense no-caps").classes(
                    "hp-row-button hp-confirm-danger w-auto" if danger else "hp-send hp-dialog-ok w-auto"
                )

                async def _ok() -> None:
                    """确认：执行回调（可 async）→ 关框；执行期间整框禁操作（按钮转圈、
                    取消/×/ESC 都关不掉——回调跑到一半框没了，状态就和界面脱节了）。"""
                    button.props("loading")
                    button.disable()
                    cancel_button.disable()
                    dialog.props("no-esc-dismiss no-backdrop-dismiss")
                    try:
                        await _call(on_confirm)
                    finally:
                        button.props(remove="loading")
                        button.enable()
                        cancel_button.enable()
                        dialog.props(remove="no-esc-dismiss no-backdrop-dismiss")
                        _destroy(dialog)
                        _clear()

                button.on_click(_ok)
                if confirm_mark:
                    button.mark(confirm_mark)
        dialog.on("hide", _clear)
        dialog.on("close", _clear)
        # 按 ESC / 点遮罩关闭时也要删元素，否则每开一次就留一份在 DOM 里
        dialog.on("hide", lambda: _destroy(dialog))
    _ACTIVE[key] = (token, dialog)
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
        actions: Quasar notify 的 ``actions``（形如 ``[{"label": "撤销"}]``）。
            **只能放 JSON 可序列化的值**：Quasar 的 ``handler`` 是 JS 函数，传 Python
            函数会在序列化时抛 ``Type is not JSON serializable: function``（实测）；
            需要 Python 回调请用行内按钮/对话框，不要走 notify。
        timeout: 自动消失毫秒数；None 时按 kind 给默认——成功短驻、错误停留更久，
            但**都有上限**（此前非 positive 是 None = 永不消失，高频操作会堆一屏 toast）。
    """
    default_timeout = {"positive": 3500, "info": 5000, "warning": 6000, "negative": 8000}
    resolved = timeout if timeout is not None else default_timeout.get(kind, 5000)
    if actions:
        # 防御：含 callable 的 action 一定发不出去，直接丢弃并报错（别让整页 500）
        serializable = [a for a in actions if not any(callable(v) for v in a.values())]
        if len(serializable) != len(actions):
            logger.error("notify actions 含 Python 函数，已丢弃（Quasar 只接受 JS 回调）")
        actions = serializable or None
    if actions:
        resolved = 0  # 需要用户回应（跳转等）的不自动消失
    ui.notify(
        message,
        type=kind,
        close_button="关闭" if closeable else None,
        actions=actions,
        timeout=resolved,
        position="top-right",
        classes="hp-toast",
    )


def custom(
    *,
    kind: str = "info",
    title: str | None = None,
    content: Callable[[Callable[[], None]], None] | None = None,
    width: str = "",
) -> tuple[ui.dialog, Callable[[], None]]:
    """复杂内容的弹窗（任务详情这类：正文不是一句 message，而是时间线/列表）。

    用法：

        def body(close: Callable[[], None]) -> None:
            ui.label("…")
            ui.button("关闭", on_click=close)

        dialog, close = custom(title="任务详情", content=body)
        dialog.open()

    **正文必须画在卡片里**：``q-dialog`` 的内层是 flex 行布局，直接挂在 ``dialog`` 上的
    内容会被挤到卡片旁边、看起来像"弹窗是空的"（实测）。所以这里用回调，在卡片槽位里画。

    参数：
        width: 想比默认（460px）更宽时给 CSS 尺寸，如 ``"760px"``。检索追踪的详情要
            放多列元信息/时间线，默认太窄（实测排版很挤），所以这里可以放宽。
    """
    resolved = title or _TITLES.get(kind, "提示")
    style = "max-height: 82vh; overflow-y: auto;"  # 基础样式
    if width:  # 要放宽
        style += f" width: {width}; max-width: {width};"  # 显式改宽
    with (
        _dialog_parent(),
        ui.dialog() as dialog,
        ui.card().classes("hp-card hp-dialog gap-3").style(style),
    ):
        # 与 show_dialog/confirm 同一个头（图标 + 标题 + 右上角 ×）：不能只有底部"关闭"一条路
        _dialog_head(kind, resolved, dialog, on_close=lambda: _destroy(dialog))
        ui.separator()
        if content is not None:  # 正文在卡片槽位里画
            content(lambda: _destroy(dialog))  # 传入"关闭"动作，正文里的按钮要用
    # 关闭时**删除元素**：任务详情/片段详情每点一次都新建一个，不删会越点越卡
    dialog.on("hide", lambda: _destroy(dialog))
    return dialog, lambda: _destroy(dialog)
