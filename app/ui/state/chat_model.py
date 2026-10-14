"""聊天区的**数据形状**（渲染器只认这几个类）。

为什么把它和"数据从哪来"分开：现在数据是模拟的（`app/screens/_chat_mock.py`），
以后是检索结果 + 模型生成的回答。渲染器只依赖这里的形状，换数据源不用改渲染代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fragment:
    """一条引用片段（点角标后在右栏看全文）。"""

    index: int
    source_name: str
    origin: str
    """出处：第几段 / 第几页。"""

    score: float
    """bm25 分（只用于排序与显示，不是 0~1 相似度）。"""

    text: str
    """片段正文，按 Markdown 渲染。"""


@dataclass(frozen=True)
class Process:
    """过程行（可收起的那一行）。"""

    running: str
    """运行中的文案。"""

    done: str
    """完成后的文案。"""

    details: list[str] = field(default_factory=list)
    """展开后看到的东西（查询词、每条命中的分数…）。"""


@dataclass(frozen=True)
class Turn:
    """一轮答复 = 过程行 + 答案 + 引用片段（或一句说明）。"""

    process: Process | None = None
    answer: str = ""
    """答案正文，Markdown。空串表示这轮没有答案（无命中 / 报错）。"""

    fragments: list[Fragment] = field(default_factory=list)
    note: str = ""
    """没有答案时的那句话（无命中 / 报错）。"""

    note_kind: str = "plain"
    """``plain`` 或 ``error``——报错要用语义色，不能和"没找到"长一样。"""


@dataclass(frozen=True)
class Entry:
    """一轮对话：用户问了什么 + 这一轮的答复。"""

    question: str
    turn: Turn
