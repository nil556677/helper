"""待发送消息队列（**UI-free**，可直接单测）。

聊天页的发送队列只存"还没开始执行"的消息：正在执行的那条会先出队，所以不在队列里。
这里刻意不依赖 NiceGUI，方便对**边界与异常**做单元测试；渲染与执行仍由
``app/ui/screens/chat.py`` 负责。

规则（与界面文案一致）：

- 队列是**内存级**：刷新/切页即丢；
- 最多 :data:`QUEUE_MAX_ITEMS` 条；
- 所有排队消息的字符总数不得超过 :data:`QUEUE_MAX_CHARS`；
- **以先到的为准**：满了只拒绝新消息，不挤掉已排队的；
- 空消息不允许入队；编辑成空同样不允许。
"""

from __future__ import annotations  # 类型注解延迟求值

from dataclasses import dataclass, field  # 数据类
from typing import Any  # 任意

#: 队列上限：条数（正在执行的那条不占名额）。
QUEUE_MAX_ITEMS = 10

#: 队列上限：所有排队消息的字符总数。
QUEUE_MAX_CHARS = 10_000


@dataclass
class QueuedMessage:
    """一条待发送消息：文本 + 发送时所在的会话 id。

    刻意**可变**：编辑时原地改 ``text``，保持对象身份不变，界面手里的引用才不会失效。
    """

    text: str
    conversation_id: str
    attachments: list[dict[str, Any]] = field(default_factory=list)  # 附件


class MessageQueue:
    """内存级待发送队列：入队/出队/删除/编辑 + 上限检查。

    所有“拒绝”都用**返回提示文案**表达（``None`` 表示成功），调用方直接拿去
    ``notify``，不需要捕获异常；类型错误（非字符串）则显式抛 ``TypeError``，
    避免把脏数据静默塞进队列。
    """

    def __init__(
        self,
        *,
        max_items: int = QUEUE_MAX_ITEMS,
        max_chars: int = QUEUE_MAX_CHARS,
    ) -> None:
        # 构造参数可注入，便于测试用小上限快速覆盖边界。
        if max_items <= 0 or max_chars <= 0:
            raise ValueError("队列上限必须为正数")
        self._items: list[QueuedMessage] = []
        self.max_items = max_items
        self.max_chars = max_chars

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def items(self) -> tuple[QueuedMessage, ...]:
        """返回当前排队的快照（tuple，外部改不了内部列表）。"""
        return tuple(self._items)

    @property
    def chars(self) -> int:
        """当前排队消息的总字符数。"""
        return sum(len(item.text) for item in self._items)

    def reject_reason(self, text: str, *, exclude: QueuedMessage | None = None) -> str | None:
        """检查一条消息能否入队；能则返回 ``None``，否则返回给用户看的提示。

        ``exclude`` 用于“编辑已有条目”：统计总量时先把它自己排除，避免重复计算。
        """
        # 路径 1：非字符串 → 显式报错，不让 None/bytes 混进来。
        if not isinstance(text, str):
            raise TypeError(f"消息必须是 str，收到 {type(text).__name__}")
        stripped = text.strip()
        # 路径 2：空白消息 → 拒绝（调用方通常已 strip，这里兜底）。
        if not stripped:
            return "空消息不能加入待发送队列。"
        candidates = [item for item in self._items if item is not exclude]
        # 路径 3：条数到顶 → 拒绝新消息，已排队的不动（先到为准）。
        if len(candidates) >= self.max_items:
            return (
                f"待发送队列已满（最多 {self.max_items} 条），这条没有加入；"
                "请等前面的发送完再试。"
            )
        used = sum(len(item.text) for item in candidates)
        # 路径 4：总字符数会超限 → 拒绝新消息。
        if used + len(stripped) > self.max_chars:
            return (
                f"待发送内容总长超过上限（{self.max_chars} 字符，当前 {used}），"
                "这条没有加入；请等前面的发送完再试。"
            )
        # 路径 5：允许。
        return None

    def push(  # 入队
        self,
        text: str,
        conversation_id: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """尝试入队；成功返回 ``None``，被拒返回提示文案（此时队列不变）。"""
        files = list(attachments or [])  # 附件
        content = text.strip() or ("请查看附件。" if files else "")  # 只有附件时给占位
        reason = self.reject_reason(content)
        if reason is not None:
            return reason
        self._items.append(QueuedMessage(text=content, conversation_id=conversation_id, attachments=files))
        return None

    def pop(self) -> QueuedMessage | None:
        """取出队首（出队即“开始执行”）；队列空返回 ``None``。"""
        if not self._items:
            return None
        return self._items.pop(0)

    def remove(self, item: QueuedMessage) -> bool:
        """按**对象身份**删除一条；删到了返回 ``True``，不在队列返回 ``False``。"""
        for index, existing in enumerate(self._items):
            if existing is item:
                del self._items[index]
                return True
        return False

    def clear(self, conversation_id: str | None = None) -> int:
        """清空排队（可只清某个会话）；返回删掉的条数。正在执行的那条不在这里。"""
        if conversation_id is None:  # 全清
            removed = len(self._items)  # 条数
            self._items.clear()  # 清
            return removed  # 返回
        before = len(self._items)  # 原条数
        self._items = [item for item in self._items if item.conversation_id != conversation_id]  # 只留别的会话
        return before - len(self._items)  # 删了几条

    def replace_text(self, item: QueuedMessage, text: str) -> str | None:
        """编辑一条尚未执行的消息；成功返回 ``None``，被拒返回提示文案。

        用**原地修改**保持对象身份不变（调用方手里的 ``item`` 仍然有效），
        这样界面上的编辑/删除按钮不会因为替换对象而指向旧条目。
        """
        reason = self.reject_reason(text, exclude=item)
        if reason is not None:
            return reason
        for existing in self._items:
            if existing is item:
                item.text = text.strip()
                return None
        # 路径：条目已经不在队列（例如刚好被 _drain 取走）→ 明确告知，不静默改。
        return "这条消息已经不在待发送队列里了。"
