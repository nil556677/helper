"""聊天区的**数据形状**（渲染器只认这几个类）。

为什么把它和"数据从哪来"分开：数据源（`app/rag/assemble.py`）负责
检索与拼 Turn，渲染器（`app/ui/screens/chat.py`）只依赖这里的形状；换数据源
（比如以后接生成式 LLM 的流式回答）不用改渲染代码。

一轮答复 = 用户问题（`Entry.question`）+ 过程组（`Process`：步骤 + 推理预览）
+ 答案（`Turn.answer`，Markdown）+ 引用（`Turn.fragments`）。
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

    snippet: str = ""
    """**命中的那一小段**（child 原文）。侧栏用它指出"到底哪一段命中了"，
    和上面的 ``text``（父块全文/证据正文）区分开。"""
    # 路径：snippet 为空 / 与 text 相同 → 侧栏只显示 text，不重复展示"命中片段"块。

    score_kind: str = "cosine"
    """分数语义：``cosine``（单路语义）/ ``bm25``（单路关键词）/ ``rrf``（多路融合）。

    **不能混标**：RRF 是名次分（约 0.02），不是 bm25，也不是余弦。
    """


@dataclass(frozen=True)
class Step:
    """过程里的一步（时间线上的一行）：接口统一，实时与历史共用同一份数据。

    设计要点（A+B 融合）：步骤**按发生顺序**存列表，不再按固定分组归并——
    实时视图与历史重画都读这同一份 `steps`，保证"一次事实、前后对应"。
    """

    group: str = ""
    """分组（``prepare`` / ``search`` / ``filter`` / ``tool`` / ``generate``），只用来选图标。"""

    title: str = ""
    """一行标题（人话，如"查找资料"）。"""

    detail: str = ""
    """展开后看到的内容（参数 / 结果 / 原因）。"""
    # 路径：detail 为空 → 步骤行展开时显示"（无明细）"，不会出现空白面板。

    status: str = "done"
    """``running`` / ``done`` / ``failed`` / ``skipped``——决定圆点与图标颜色。"""
    # 路径：failed 用错误色并带 ✗；running 高亮并带转圈；其余按成功样式。

    ms: int = 0
    """这一步的真实耗时（毫秒）；0 表示未知（不显示）。"""

    children: list[Step] = field(default_factory=list)
    """子步骤（工具调用的参数/结果等），渲染时缩进挂在父步骤下面。"""
    # 路径：空列表 = 没有子项；有子项时父步骤行右侧显示数量。


@dataclass(frozen=True)
class Process:
    """过程组（可收起的那块）：步骤列表 + 推理预览。"""

    running: str
    """运行中的文案。"""
    # 路径：等待阶段显示 running；检索完成后整轮被替换，running 不再出现。

    done: str
    """完成后的文案（也用作没有步骤时的兜底标题）。"""
    # 路径：steps 为空时，标题用 done；有步骤时标题由各步骤名拼成。

    steps: list[Step] = field(default_factory=list)
    """这轮做过的步骤（按发生顺序）。"""
    # 路径：空列表 = 只有推理/答案、没有步骤明细；渲染器会跳过步骤循环。

    reasoning: str = ""
    """推理预览：本地检索口径的理解与依据。

    **不是模型思维链**（本应用还没接生成式 LLM）：它写清本轮"按什么模式、什么
    阈值、命中多少、回填多少"。接 LLM 后这里换成模型流式的 reasoning。
    """
    # 路径：空串 → 过程组里不渲染"推理"折叠块；非空才渲染。

    duration_ms: float | None = None
    """本轮总耗时（毫秒）；没有可测的耗时就留 ``None``。"""
    # 路径：None → 标题里不显示耗时；有值才拼 " · 123ms"。


@dataclass(frozen=True)
class Turn:
    """一轮答复 = 过程行 + 答案 + 引用片段（或一句说明）。"""

    process: Process | None = None
    # 路径：None → 不画过程组（例如纯错误提示）；非 None 才渲染可折叠过程。
    answer: str = ""
    """答案正文，Markdown。空串表示这轮没有答案（无命中 / 报错）。"""
    # 路径：answer 非空 → 画 Markdown + 引用；为空 → 走 note 分支。

    fragments: list[Fragment] = field(default_factory=list)
    # 路径：空列表 → 不画"资料"折叠行；非空才画。
    note: str = ""
    """没有答案时的那句话（无命中 / 报错）。"""
    # 路径：answer 为空且 note 非空 → 显示这句；两者都空 → 这一轮什么都不显示。

    note_kind: str = "plain"
    """``plain`` 或 ``error``——报错要用语义色，不能和"没找到"长一样。"""
    # 路径：note_kind == "error" → 用错误样式；其它值用弱提示样式。

    basis: dict = field(default_factory=dict)
    """检索依据：关键词 / 计数 / 来源。"""

    settings: dict = field(default_factory=dict)
    """本轮设置：模型 / 温度 / 系统提示词。"""

    usage: dict = field(default_factory=dict)
    """本轮用量：耗时 / 字数 / 引用数。"""

    conflict: str = ""
    """资料与通用知识冲突时的提示。"""

    context_usage: dict = field(default_factory=dict)
    """本轮上下文占用（window/tiers/blocks/...；来自 CONTEXT 事件）。"""


@dataclass(frozen=True)
class Entry:
    """一轮对话：用户问了什么 + 这一轮的答复。"""

    question: str
    turn: Turn
    attachments: list[dict] = field(default_factory=list)  # 附件
