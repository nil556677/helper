"""会话列表与当前会话（**界面态**）。

**现状：只在内存里**，进程退出就没了——因为后台数据现在是模拟的（用户要求先只做界面）。
接真实后端时换掉这个文件的实现即可：`conversations` / `messages` 两张表 schema v1 已经建好，
而对外这几个函数的签名不用变。

**两个订阅口是故意分开的**：

- :func:`subscribe_list`：会话**列表**变了（新建、自动命名、又追加了一轮）→ 外壳重画左栏；
- :func:`subscribe_select`：**当前会话**换了 → 聊天页整块重画对话流。

合成一个的话，每发一条消息都会把对话流整块重画一遍——动画白做，滚动位置也可能跳。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from app.ui.state.chat_model import Entry

#: 新会话的默认标题（用户问了第一句之后会被替换成那句话）
DEFAULT_TITLE = "新对话"

#: 自动命名时标题最多几个字
TITLE_MAX_CHARS = 18


@dataclass
class Conversation:
    """一个会话：标题 + 若干轮对话 + 一句可选的说明。"""

    id: str
    title: str = DEFAULT_TITLE
    entries: list[Entry] = field(default_factory=list)
    created_at: float = 0.0
    note: str = ""
    """会话级说明（示例会话用它标注"这一轮是模拟数据"）。"""


_conversations: dict[str, Conversation] = {}
_order: list[str] = []
_current_id: str | None = None
_list_listeners: list[Callable[[], None]] = []
_select_listeners: list[Callable[[], None]] = []


def conversations() -> list[Conversation]:
    """全部会话（**新的在前**）。"""
    return [_conversations[cid] for cid in _order]


def current() -> Conversation:
    """当前会话；一个都没有时自动建一个（界面不该处理"没有当前会话"这种状态）。"""
    global _current_id
    if _current_id is None or _current_id not in _conversations:
        return new()
    return _conversations[_current_id]


def new(*, title: str = DEFAULT_TITLE, note: str = "", select_it: bool = True) -> Conversation:
    """新建一个会话。

    参数：
        title: 标题（留空就用默认标题，等第一句问题自动命名）。
        note: 会话级说明。
        select_it: 是否立刻切过去（默认是；种子会话会传 False）。
    返回：新会话。
    """
    global _current_id
    conversation = Conversation(
        id=uuid.uuid4().hex[:12], title=title, created_at=time.time(), note=note
    )
    _conversations[conversation.id] = conversation
    _order.insert(0, conversation.id)
    if select_it:
        _current_id = conversation.id
        _notify_select()
    _notify_list()
    return conversation


def select(conversation_id: str) -> None:
    """切到某个会话（不存在就忽略）。"""
    global _current_id
    if conversation_id not in _conversations or conversation_id == _current_id:
        return
    _current_id = conversation_id
    _notify_select()
    _notify_list()  # 列表里要把"当前"这一项点亮


def append(entry: Entry) -> None:
    """给当前会话追加一轮对话（顺带按第一句问题自动命名）。"""
    conversation = current()
    conversation.entries.append(entry)
    if conversation.title == DEFAULT_TITLE and entry.question.strip():
        conversation.title = _short_title(entry.question)
    # 只通知列表：对话流是调用方**自己增量画的**（那里有过程行动画，重画会把动画抹掉）
    _notify_list()


def reset() -> None:
    """清空全部会话**并清掉订阅者**（种子与测试用）。

    为什么连订阅者一起清：订阅者是上一轮界面注册进来的回调，那些元素已经不存在了；
    留着它们，下一次通知就会去操作已删除的元素（NiceGUI 会直接报错）。
    """
    global _current_id
    _conversations.clear()
    _order.clear()
    _current_id = None
    _list_listeners.clear()
    _select_listeners.clear()


def subscribe_list(callback: Callable[[], None]) -> None:
    """注册"列表变了"的回调（外壳用）。"""
    _list_listeners.append(callback)


def subscribe_select(callback: Callable[[], None]) -> None:
    """注册"当前会话换了"的回调（聊天页用）。"""
    _select_listeners.append(callback)


def _short_title(question: str) -> str:
    """用第一句问题当标题（超长截断）。"""
    text = " ".join(question.split())
    return text if len(text) <= TITLE_MAX_CHARS else text[:TITLE_MAX_CHARS] + "…"


def _notify_list() -> None:
    for callback in list(_list_listeners):
        callback()


def _notify_select() -> None:
    for callback in list(_select_listeners):
        callback()
