"""块类型 → 切分策略（**唯一出处**）。

## 在流水线中的位置

解析（P1.1）产出 ``Block``（带 ``content_type``）→ 结构识别（P1.2）标出标题 →
**切分（P1.3）** 用本模块把"类型"翻译成"怎么处理" → 入库（P1.4）。

## 输入 / 输出

- 输入：``Block.content_type``（字符串）与可选策略表。
- 输出：:class:`Strategy` 枚举值（``boundary`` / ``split`` / ``row_split`` / ...）。

## 关键约定

1. **加一种块类型只加一行**；策略是"唯一出处"，别在 ``Splitter`` 里再写 if/else；
2. **未知类型按正文切**（``SPLIT``）：宁可多切，也不要静默丢内容；
3. 不同领域（rag / memory / tools）可以传自己的策略表；默认表 ``POLICY`` 是 rag 口径。
"""

from __future__ import annotations

from enum import StrEnum


class Strategy(StrEnum):
    """一个块类型怎么处理。

    用 ``StrEnum`` 而不是普通 ``Enum``：日志、诊断、JSON 里都能直接当字符串用，
    不需要再维护一张"枚举 → 文本"的映射表。
    """

    BOUNDARY = "boundary"
    """只作 parent 边界，不产片段（标题）。"""

    SPLIT = "split"
    """按句子/软边界递归切分（正文、列表项、引用）。"""

    ROW_SPLIT = "row_split"
    """表格：按整行切，每段重复表头。"""

    ATOMIC_LINES = "atomic_lines"
    """代码：不按标点切，只能按整行切。"""

    SKIP = "skip"
    """不产片段（图片，待 OCR）。"""


#: 默认策略表（rag 用）。键是 ``Block.content_type``。
POLICY: dict[str, Strategy] = {
    "heading": Strategy.BOUNDARY,
    "text": Strategy.SPLIT,
    "paragraph": Strategy.SPLIT,
    "list_item": Strategy.SPLIT,
    "quote": Strategy.SPLIT,
    "table": Strategy.ROW_SPLIT,
    "code": Strategy.ATOMIC_LINES,
    "image": Strategy.SKIP,
    # 作者/单位：独立成块（短，不并进正文）
    "byline": Strategy.SPLIT,
}


def strategy_for(content_type: str, policy: dict[str, Strategy] | None = None) -> Strategy:
    """查一个块类型该怎么切（未知类型按正文处理）。

    参数：
        content_type: ``Block.content_type``（如 ``"text"`` / ``"table"``）。
        policy: 可选策略表；不传用默认的 :data:`POLICY`。
    返回：对应的 :class:`Strategy`；表里没有这个类型时返回 ``SPLIT``。
    说明：**未知类型不抛异常**——解析层以后新增类型时，切分仍能安全地按正文处理。
    """
    return (policy or POLICY).get(content_type, Strategy.SPLIT)
