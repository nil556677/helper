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

from loguru import logger

from app.ui.state.chat_model import Entry

#: 新会话的默认标题（用户问了第一句之后会被替换成那句话）
DEFAULT_TITLE = "新对话"

#: 自动命名时标题最多几个字
TITLE_MAX_CHARS = 18

#: 手动改名时标题最多几个字（自动命名上限 18，手动可更长）
RENAME_MAX_CHARS = 40


@dataclass
class Conversation:
    """一个会话：标题 + 若干轮对话 + 一句可选的说明。"""

    id: str
    title: str = DEFAULT_TITLE
    entries: list[Entry] = field(default_factory=list)
    created_at: float = 0.0
    note: str = ""
    """会话级说明（示例会话用它标注"这一轮是模拟数据"）。"""
    # 路径：note 非空 → 聊天页顶部画一条提示；空则不画。


# 模块级单例状态（单机单窗口；多窗口时换成 client 级存储）。
_conversations: dict[str, Conversation] = {}  # id → 会话对象（查得快）
_order: list[str] = []  # id 顺序（新的在前，决定左栏顺序）
_current_id: str | None = None  # 当前选中的会话 id
_list_listeners: list[Callable[[], None]] = []  # "列表变了"的订阅者
_select_listeners: list[Callable[[], None]] = []  # "当前会话换了"的订阅者


def conversations() -> list[Conversation]:
    """全部会话（**新的在前**）。"""
    # 路径：按 _order 取出对象；_order 已保证新的在前，这里不再排序。
    return [_conversations[cid] for cid in _order]


def current() -> Conversation:
    """当前会话；一个都没有时自动建一个（界面不该处理"没有当前会话"这种状态）。"""
    global _current_id
    # 路径 1：还没选过 / 选中的会话已被删（reset 后）→ 自动新建一个空会话。
    if _current_id is None or _current_id not in _conversations:
        return new()
    # 路径 2：正常返回当前会话。
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
    # 用短 uuid 当 id：够唯一、又不会把日志/界面撑长。
    conversation = Conversation(
        id=uuid.uuid4().hex[:12], title=title, created_at=time.time(), note=note
    )
    _conversations[conversation.id] = conversation
    _order.insert(0, conversation.id)  # 新会话永远排最前
    # 新建即落库：前端有、后端也必须有（否则重启后空会话丢失、前后端不对应）。
    try:
        from app.services import conversations as service  # 延迟导入

        service.ensure_conversation(conversation.id, title=title)  # 建行
    except Exception:  # 库异常不阻断界面
        pass  # 静默（首条消息仍会补建）
    # 路径 1：select_it=True → 切到新会话，并通知"当前会话变了"（聊天页重画）。
    if select_it:
        _current_id = conversation.id
        _notify_select()
    # 路径 2：select_it=False → 只新增、不切换（种子会话/后台建会话用）。
    # 无论切不切，列表都变了（左栏要重画）。
    _notify_list()
    return conversation


def ensure_seeded() -> None:
    """第一次进来时建一个**空会话**（有会话就不动）。"""
    if conversations():
        return
    restore_from_db()  # 先从库里恢复历史
    if conversations():
        return  # 恢复到了
    new(title="新对话")


def _turn_from_json(content: str, raw: object) -> object:  # 还原一轮
    """把 turn_json 还原成 Turn；坏数据退回纯文本，绝不因历史坏数据启动失败。"""
    import json  # 延迟导入

    from app.ui.state.chat_model import Fragment, Process, Step, Turn  # 形状

    if not raw:  # 没有 turn_json
        return Turn(answer=content)  # 纯文本
    try:  # 整体兜底
        data = json.loads(str(raw))  # 解析
        if not isinstance(data, dict):  # 不是对象
            return Turn(answer=content)  # 退回
        steps: list[Step] = []  # 步骤
        for item in data.get("steps") or []:  # 遍历
            if not isinstance(item, dict):  # 坏项
                continue  # 跳过
            title = str(item.get("title") or "")  # 标题
            if not title:  # 没标题
                continue  # 跳过
            steps.append(  # 追加
                Step(  # 步骤
                    group=str(item.get("group") or item.get("kind") or "search"),  # 分组（旧数据是 kind）
                    title=title,  # 标题
                    detail=str(item.get("detail") or ""),  # 明细
                    body=str(item.get("body") or ""),  # 展开正文
                    status=str(item.get("status") or "done"),  # 状态
                    ms=int(item.get("ms") or 0),  # 耗时
                    children=[  # 子项（工具参数/结果，递归还原）
                        Step(  # 子步骤
                            group=str(c.get("group") or "tool"),  # 分组
                            title=str(c.get("title") or ""),  # 标题
                            detail=str(c.get("detail") or ""),  # 明细
                            status=str(c.get("status") or "done"),  # 状态
                            ms=int(c.get("ms") or 0),  # 耗时
                        )
                        for c in (item.get("children") or []) if isinstance(c, dict)  # 逐个
                    ],
                )  # 步骤结束
            )  # 追加结束
        fragments: list[Fragment] = []  # 引用
        for item in data.get("fragments") or []:  # 遍历
            if not isinstance(item, dict):  # 坏项
                continue  # 跳过
            try:  # 序号/分数可能坏
                index = int(item.get("index") or 0)  # 序号
                score = float(item.get("score") or 0.0)  # 分数
            except (TypeError, ValueError):  # 坏数值
                continue  # 跳过
            if index <= 0:  # 无效序号
                continue  # 跳过
            fragments.append(  # 追加
                Fragment(  # 引用
                    index=index,  # 序号
                    source_name=str(item.get("source_name") or "资料"),  # 来源
                    origin=str(item.get("origin") or "资料"),  # 出处
                    score=score,  # 分数
                    text=str(item.get("text") or ""),  # 正文
                    snippet=str(item.get("snippet") or ""),  # 命中段
                    score_kind=str(item.get("score_kind") or "rrf"),  # 分数语义
                    doc_id=int(item.get("doc_id") or 0),  # 所属文档（查看整篇用）
                )  # 引用结束
            )  # 追加结束
        process = Process(  # 过程
            running="",  # 已完成
            done=steps[-1].title if steps else "完成",  # 标题
            steps=steps,  # 步骤
            reasoning=str(data.get("reasoning") or ""),  # 思考
        ) if steps or data.get("reasoning") else None  # 有内容才建
        return Turn(  # 返回
            process=process,  # 过程
            answer=str(data.get("answer") or content),  # 答案
            fragments=fragments,  # 引用
            note=str(data.get("note") or ""),  # 说明
            note_kind=str(data.get("note_kind") or "plain"),  # 样式
            basis=dict(data.get("basis") or {}),  # 检索依据
            settings=dict(data.get("settings") or {}),  # 本轮设置
            usage=dict(data.get("usage") or {}),  # 用量
            conflict=str(data.get("conflict") or ""),  # 冲突提示
            context_usage=dict(data.get("context_usage") or {}),  # 上下文占用
        )  # 结束
    except Exception:  # 任何坏数据
        return Turn(answer=content)  # 退回纯文本


def restore_from_db() -> None:  # 从库恢复
    """启动时把历史会话装回内存；失败静默。"""
    try:  # 库异常不影响启动
        from app.services import conversations as service  # 延迟导入

        rows = service.list_conversations()  # 会话列表
    except Exception:  # 读失败
        return  # 放弃
    for row in rows:  # 新的在前
        conversation_id = str(row.get("id") or "")  # id
        if not conversation_id:  # 空 id
            continue  # 跳过
        try:  # 单会话失败不影响其它
            messages = service.load_messages(conversation_id)  # 消息
        except Exception:  # 读失败
            messages = []  # 空
        entries: list[Entry] = []  # 轮次
        pending_question = ""  # 待配对的问题
        for message in messages:  # 逐条
            role = str(message.get("role") or "")  # 角色
            content = str(message.get("content") or "")  # 内容
            if role == "user":  # 用户
                pending_question = content  # 记下
            elif role == "assistant" and pending_question:  # 助手
                turn = _turn_from_json(content, message.get("turn_json"))  # 还原
                attachments: list[dict] = []  # 附件
                try:  # 坏数据不阻断
                    import json  # 延迟导入

                    raw_turn = json.loads(str(message.get("turn_json") or "{}"))  # 解析
                    if isinstance(raw_turn, dict) and isinstance(raw_turn.get("attachments"), list):  # 有附件
                        attachments = [item for item in raw_turn["attachments"] if isinstance(item, dict)]  # 过滤
                except (TypeError, ValueError):  # 坏数据
                    attachments = []  # 空
                entries.append(Entry(question=pending_question, turn=turn, attachments=attachments))  # 配对
                pending_question = ""  # 清空
        conversation = Conversation(  # 会话对象
            id=conversation_id,  # id
            title=str(row.get("title") or DEFAULT_TITLE),  # 标题
            entries=entries,  # 轮次
            created_at=float(row.get("created_at") or 0.0),  # 时间
        )  # 会话结束
        _conversations[conversation_id] = conversation  # 存
        _order.append(conversation_id)  # 顺序
    if _order:  # 有历史
        global _current_id  # 修改当前
        _current_id = _order[0]  # 选最新
        _notify_list()  # 刷新左栏



def select(conversation_id: str) -> None:
    """切到某个会话（不存在就忽略）。"""
    global _current_id
    # 路径 1：目标不存在，或点的就是当前会话 → 什么都不做（避免无意义重画 + 动画闪）。
    if conversation_id not in _conversations or conversation_id == _current_id:
        return
    # 路径 2：正常切换：改当前 id → 通知聊天页重画 → 通知左栏点亮当前项。
    _current_id = conversation_id
    _notify_select()
    _notify_list()  # 列表里要把"当前"这一项点亮


def delete(conversation_id: str) -> None:
    """删除会话：当前会话删掉后自动切到下一个。"""
    global _current_id
    if conversation_id not in _conversations:  # 不存在
        return  # 忽略
    was_current = conversation_id == _current_id  # 是否当前
    _conversations.pop(conversation_id, None)  # 删内存
    if conversation_id in _order:  # 删顺序
        _order.remove(conversation_id)  # 移除
    try:  # 删库失败不阻断界面
        from app.services import conversations as service  # 延迟导入

        service.delete_conversation(conversation_id)  # 删库
    except Exception:  # 库异常
        pass  # 静默
    if not _order:  # 全删光
        _current_id = None  # 清当前
        new()  # 建一个空会话
        return  # 结束
    if was_current:  # 删的是当前
        _current_id = _order[0]  # 切到最新的
        _notify_select()  # 重画聊天
    _notify_list()  # 刷新列表


def append(entry: Entry) -> None:
    """给当前会话追加一轮对话（顺带按第一句问题自动命名）。"""
    # 路径：先取当前会话（可能触发"没有就新建"），再交给 append_to 统一处理。
    append_to(current().id, entry)


def rename(conversation_id: str, title: str) -> str:
    """改会话名（也落库）；空回退默认标题，超长截断。返回最终标题。"""
    conversation = _conversations.get(conversation_id)
    if conversation is None:  # 会话不存在（已被删）
        return DEFAULT_TITLE  # 直接返回默认
    cleaned = (title or "").strip()  # 去首尾空白
    if not cleaned:  # 空 → 回退默认（写默认值，别写空串）
        cleaned = DEFAULT_TITLE  # 默认
    cleaned = cleaned[:RENAME_MAX_CHARS]  # 超长截断
    conversation.title = cleaned  # 更新内存
    try:  # 落库失败不阻断界面（重画列表也会用内存值）
        from app.services import conversations as service  # 延迟导入

        service.rename_conversation(conversation_id, cleaned)  # 写库
    except Exception:  # 库异常
        pass  # 静默
    _notify_list()  # 列表变了（左栏改名后重画）
    return cleaned  # 最终标题


def append_to(conversation_id: str, entry: Entry) -> None:
    """给**指定**会话追加一轮（顺带按第一句问题自动命名）。

    为什么需要按 id 追加：聊天页发消息后检索在后台跑，用户可能在这期间切走了会话。
    结果要落回**发出问题的那一个**会话，而不是"切过去之后的当前会话"。
    """
    conversation = _conversations.get(conversation_id)
    # 路径 1：目标会话已被删除（reset/切换期间）→ 静默丢弃这一轮，不抛错。
    if conversation is None:
        return
    # 路径 2：正常追加。
    conversation.entries.append(entry)
    # 有新一轮 → 该会话在左栏提到最前（与后端 updated_at DESC 对齐，否则顺序会"乱"）。
    if conversation_id in _order:
        _order.remove(conversation_id)  # 先摘
        _order.insert(0, conversation_id)  # 再置顶
    # 自动命名：仍是默认标题，且这一轮问题非空 → 用问题前 18 字当标题。
    # 路径 2a：标题已改过 / 问题为空 → 不改名。
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
    # 路径：追加到列表；同一回调重复注册会重复触发（调用方自己保证只注册一次）。
    _list_listeners.append(callback)


def subscribe_select(callback: Callable[[], None]) -> None:
    """注册"当前会话换了"的回调（聊天页用）。"""
    # 路径：同上，追加式注册。
    _select_listeners.append(callback)


def _short_title(question: str) -> str:
    """用第一句问题当标题（超长截断）。"""
    # 先把所有空白（含换行）压成单空格，避免标题里出现换行。
    text = " ".join(question.split())
    # 路径 1：不超长 → 原样；路径 2：超长 → 截断 + 省略号。
    return text if len(text) <= TITLE_MAX_CHARS else text[:TITLE_MAX_CHARS] + "…"


def _notify(callbacks: list[Callable[[], None]], kind: str) -> None:
    # 用 list(...) 拷贝：回调里如果又增删订阅者，不会让当前遍历漏掉/重复。
    for callback in list(callbacks):
        try:
            callback()
        except RuntimeError as exc:
            # 页面已关/浏览器已刷新：元素随客户端删除，但**数据仍要落库**。
            # 只吞“客户端已删除”这一类；其它 RuntimeError 照常抛。
            message = str(exc).lower()
            if "client" not in message and "deleted" not in message:
                raise
            logger.trace("跳过已失效的界面回调（{}）：{}", kind, exc)


def _notify_list() -> None:
    _notify(_list_listeners, "list")


def _notify_select() -> None:
    _notify(_select_listeners, "select")
