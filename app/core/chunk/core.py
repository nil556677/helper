"""切分器核心：流式 ``Splitter`` 类（标题开 parent、句级聚合 child）。

## 在流水线中的位置

解析（P1.1）产出 ``Block`` → 结构识别（P1.2）标出标题 → **切分（P1.3，本包）**
→ 入库（P1.4）。本模块是切分层的大脑：消费块、维护"当前 parent"、把 child 挂上去，
封口后把 ``ChunkGroup`` 交给入库层。

## 输入 / 输出

- 输入：``feed(blocks)`` 逐结构单元喂块；``flush()`` 收尾。
- 输出：已封口的 ``ChunkGroup``（parent 记录 + child 记录），顺序 = 文档顺序。

## 关键约定

1. **流式、内存有界**：只压当前一个 parent（≤ ``parent_size``），不攒整份文档；
2. **标题只作边界、不进 child**：标题开启 parent；连续标题中没等到正文的那个视为空节丢弃；
3. **child 不跨块/不跨段落**：块是 P1.1 的语义单元，空行是作者给的边界；
4. **预算按"嵌入文本"算**（选项 1B）：章节标题的开销先从预算里扣掉；
5. **降级必留痕**：硬切、跳过图片、超预算都写 ``self.sink``，不静默。

按主题归到：``estimate``（token 估算）、``sentences``（标点切分）、
``records``（写库记录）、``pieces``（片段候选/累积 parent）、``loc``（定位映射）、
``entry``（便捷入口）；这里只放类本体与语义兜底的惰性包装。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace

from loguru import logger

from app.core.parse.model import (
    TYPE_BYLINE,
    TYPE_TABLE,
    TYPE_TEXT,
    Block,
)

from ._shared import _MIN_CHILD_RATIO
from .derived import derive
from .diagnostics import DiagnosticSink, Severity
from .estimate import estimate_tokens, real_token_counter
from .loc import _coords_span, _line_span
from .pieces import _OpenParent, _Piece
from .policy import Strategy, strategy_for
from .records import ChunkGroup, ChunkRecord
from .sentences import _sentence_spans, soft_spans

#: 硬切回退的**最大回退长度**（字符）：预算边界前这么多字内有标点/空白就切在它后面。
_HARD_BACKOFF_CHARS = 40
#: 硬切认可的**边界字符**（标点 + 空白）：宁可在它后面切开，也不把词/句劈两半。
_HARD_BREAKS = frozenset("，。；：！？、,.。;:!? \t\r\n ！？")

#: 原子文本：日期/时间/金额/编号/URL/邮箱/版本/单位。切点落在内部时要让位。
_ATOM_RE = re.compile(
    r"https?://[^\s，。；、]+"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"
    r"|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"
    r"|\b\d+(?:\.\d+)?%"
    r"|\bv?\d+(?:\.\d+){2,}\b"
    r"|\b\d+(?:\.\d+)?(?:kg|g|mb|gb|tb|km|cm|mm)\b",
    re.IGNORECASE,
)


def _atom_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _ATOM_RE.finditer(text)]


def _inside_atom(spans: list[tuple[int, int]], index: int) -> bool:
    return any(start < index < end for start, end in spans)


def _merge_atom_spans(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """把落在原子内部的切点并向下一段，保证 URL/日期/金额不被从句标点切开。"""
    atoms = _atom_spans(text)
    if not atoms or len(spans) <= 1:
        return spans
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and _inside_atom(atoms, merged[-1][1]):
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


#: 单份文档最多允许触发多少次**语义兜底**（embedding 很贵：16 行 ≈3.8s）。
#: 对话体/小说里长引号段落会反复触发；超过上限后退回硬切并留诊断，避免一份 2 MB
#: 文本把 CPU 占满几十分钟（实测：长任务期间界面直接卡死）。
SEMANTIC_CALLS_PER_DOC = 100


def _last_break_before_end(segment: str) -> int:
    """``segment`` 里"从后往前找、能在回退窗口内命中的最后一个边界"的位置。

    返回作为新长度的**切点**（即边界字符之后那个下标，1 基）；窗口内没有
    任何边界（或整段就是一个词）时返回 0（表示不回退）。
    """
    window_start = max(0, len(segment) - _HARD_BACKOFF_CHARS)
    atoms = _atom_spans(segment)
    for index in range(len(segment), window_start, -1):
        if segment[index - 1] in _HARD_BREAKS and not _inside_atom(atoms, index):
            return index
    return 0


class Splitter:
    """**流式**切分器：喂块进去，吐出已封口的 parent/child 组。

    为什么是有状态的类：parent 由标题开启、可能横跨多个结构单元（PDF 一页、docx 一个元素……），
    所以缓冲必须跨 ``feed()`` 保留；同时内存只压**当前一个 parent**（≤ parent_size），
    不是整份文档的片段列表——这是"大文件不整包进内存"在切分这层的延续。

    使用约定：一个实例只切**一份文档**（``feed`` 若干次 + 最后一次 ``flush``）；
    不保证线程安全，也不要在 ``flush`` 后复用它切第二份文档。
    """

    def __init__(
        self,
        *,
        parent_size: int,
        parent_overlap: int,
        child_size: int,
        child_overlap: int,
        count_tokens: Callable[[str], int] | None = None,
        semantic: Callable[[str, int], list[str] | None] | None = None,
        embed_budget: int | None = None,
        prepend_section: bool = False,
        min_child_size: int = 0,
    ) -> None:
        """构造切分器（尺寸一般由 :meth:`from_config` 推导，不手填）。

        参数：
            parent_size: parent（证据块）token 上限。
            parent_overlap: 超长节再分 parent 时的整句重叠 token 数。
            child_size: child（检索单位）token 上限。
            child_overlap: 相邻 child 之间的整句重叠 token 数。
            count_tokens: 数 token 的函数；不传则用字符估算。
            semantic: 语义兜底回调 ``(text, budget) -> list[str] | None``；None = 未启用。
            embed_budget: **嵌入文本**的硬上限；None 时取 ``child_size``。
            prepend_section: 嵌入时是否拼最内层章节标题（影响每块的预算扣减）。
            min_child_size: child 软下限；0 = 自动取 ``child_size × 40%``。
        返回：无。
        异常：构造本身不做 I/O、不抛异常；尺寸的合法性由调用方（或 ``derive``）保证。
        """
        self._parent_size = parent_size
        self._parent_overlap = parent_overlap
        self._child_size = child_size
        self._child_overlap = child_overlap
        self._count = count_tokens or estimate_tokens
        self._semantic = semantic
        #: 单文档语义兜底调用计数（防"长引号段落 × 大文本"把 embedding 打满）
        self._semantic_calls = 0
        self._semantic_cap_logged = False
        #: **嵌入文本**的硬上限（选项 1B）：候选块按 ``index_text()`` 判预算，
        #: 所以章节标题的开销被自动扣掉、标题永远进得了向量。默认取 child_size。
        self._embed_budget = embed_budget if embed_budget is not None else child_size
        self._prepend_section = prepend_section
        #: 子块下限（软）：允许**同一 parent 内**跨块合并（不跨章节），避免碎块。
        #: 0 = 自动取 40% 上限。
        self._min_child = min_child_size or int(child_size * _MIN_CHILD_RATIO)
        self._open: _OpenParent | None = None
        #: 已经生成过摘要的表格（同一个 table_ref 只生成一次，避免每批都塞一遍）
        self._table_summary_done: set[str] = set()
        #: 当前 parent 里**最后一条 child 的起始字符偏移**（块内坐标）。
        #: 合并短尾块时要用它作为合并后的起点；用"上一片段的起点"会把前一条
        #: child 的前半截丢掉（实测：日志只剩尾块、前面几百字从子块里消失）。
        self._last_child_start: int | None = None
        #: 待定标题**链**（从粗到细）：H2 后紧跟 H3 时 H2 不能被覆盖，
        #: 它要作为 parent 证据的一部分；正文到来时整条链一起写进 parent。
        self._pending: list[Block] = []
        #: 当前这条待定链是否已经被某个 parent 消费过（用于"空节"诊断）
        self._pending_used = False
        #: 父块的**章节层级**：第一个出现的标题层级（通常是 H2）。只有"≤ 这一级"的标题
        #: 才开新父块；更深的小节（H3/H4）并进同一个父块——这是"动态父块"的关键。
        self._parent_level: int | None = None
        #: 当前父块的章节路径（H2 那条）：父块因预算溢出被切开后，下一段要沿用同一条路径。
        self._parent_path: tuple[str, ...] = ()
        #: 文档大标题（H1）：标题路径从它开始，前缀预算要算进去
        self._doc_title = ""
        #: 表格序号：同一个 table_ref 在一次导入里可能多次出现（空行分段），用它保唯一
        self._table_seq = 0
        self._groups: list[ChunkGroup] = []
        #: 诊断收集器：降级/异常写进任务事件（不静默）
        self.sink = DiagnosticSink()

    # ---- 对外 ----

    @classmethod
    def from_config(
        cls, *, semantic: Callable[[str, int], list[str] | None] | None = None
    ) -> Splitter:
        """按**当前模型能力**构造（切分尺寸由嵌入宽度推导，不再读配置）。

        尺寸推导见 :mod:`.derived`：唯一外部输入是**模型自己的固定输入宽度**
        ``PAD_TO``（``app/core/embed/model.py``），其余尺寸都是围绕它的固定比例——
        子块必须塞得进嵌入宽度，手填只会静默截断。

        ``chunk.semantic_fallback`` 打开时挂上语义兜底（**惰性**：只有真的遇到
        "无结构 + 超预算长段落"才 import/加载嵌入模型）。

        参数：
            semantic: 显式注入的语义兜底；不传且配置开启时，自动挂惰性包装。
        返回：按当前模型能力配置好的 :class:`Splitter`。
        """
        from app.core.config import store as config_store
        from app.core.embed import PAD_TO

        config = config_store.load()
        counter = real_token_counter()
        if semantic is None and bool(config["chunk.semantic_fallback"]):
            semantic = _semantic_runner(counter)
        # 尺寸全部由模型的固定输入宽度推导（不是用户配置）
        sizes = derive(PAD_TO)
        return cls(
            # 父块 token 上限：送给模型的"证据"块 = child_size × 5
            parent_size=sizes.parent_size,
            # 父块超长再分时的整句重叠 = parent_size × 10%
            parent_overlap=sizes.parent_overlap,
            # 子块 token 上限：检索的最小单位 = pad_to − 安全余量
            child_size=sizes.child_size,
            # 子块之间的整句重叠 = child_size × 10%（边界句两边都在，防"答案在缝里"）
            child_overlap=sizes.child_overlap,
            # token 计量器：真 bge-m3 分词器；拿不到时 Splitter 内部退回字符估算（×0.69）
            count_tokens=counter,
            # 语义兜底：只对"无结构文档 + 单句超预算"生效（惰性加载模型）；None = 未启用
            semantic=semantic,
            # 选项 1B：预算按**嵌入文本**算（含章节标题）；child_size 已保证 ≤ pad_to
            embed_budget=sizes.child_size,
            # 嵌入/精排时是否在最前面拼上最内层章节标题（标题开销已从上面这条预算里扣掉）
            prepend_section=bool(config["chunk.prepend_section"]),
            # 子块软下限 = child_size × 40%（太短的尾块并回前一块，只在同一块内）
            min_child_size=sizes.min_child_size,
        )

    def set_doc_title(self, title: str) -> None:
        """告知文档大标题（H1）：之后算前缀预算时，标题路径从它开始。

        流式导入时标题在第一个单元就能拿到，所以每次 ``feed`` 前同步一次即可；
        必须在切片之前设置，否则早期块的预算会少算标题长度。
        """
        self._doc_title = str(title or "").strip()

    def feed(self, blocks: list[Block]) -> list[ChunkGroup]:
        """吃一个结构单元的块，返回**已封口**的组（可能为空）。

        参数：
            blocks: 一个结构单元内的块（按文档顺序；PDF 一页 / docx 一个元素……）。
        返回：本次调用中封口的 ``ChunkGroup`` 列表；没有封口时返回 ``[]``。
        说明：**流式的关键**——未封口的 parent 留在 ``self._open`` 里跨调用存活，
            调用方可以喂完一个单元就把返回的组写库，不必等整份文档进内存。
        """
        for block in blocks:
            self._consume(block)
        return self._drain()

    def flush(self) -> list[ChunkGroup]:
        """收尾：把还开着的 parent 封口（挂起的空标题丢弃但**留诊断**）。

        返回：收尾时封口的 ``ChunkGroup``（可能为空）。
        说明：**文档结束时必须调用**，否则最后一个 parent 永远不会产出；
            挂起的标题没等到正文，按"空节"丢弃（与 ``_consume`` 的标题约定一致），
            但会记一条 ``heading_without_body``，用户能在任务详情里看到。
        """
        self._close_parent()
        if self._pending and not self._pending_used:
            self.sink.add(
                "heading_without_body",
                Severity.INFO,
                "有标题始终没有等到正文，未产出片段",
                detail=self._pending[-1].text[:80],
            )
        self._pending = []
        self._pending_used = False
        return self._drain()

    # ---- 内部：消费块 ----

    def _drain(self) -> list[ChunkGroup]:
        """取走并清空已封口的组（``feed`` / ``flush`` 的返回都经过这里）。

        返回：本次累积的 ``ChunkGroup`` 列表（可能是空列表）。
        说明：先整体换出再重置内部列表，调用方拿到的列表不会被后续 ``feed`` 改动。
        """
        groups, self._groups = self._groups, []
        return groups

    def _consume(self, block: Block) -> None:
        """按策略消费一个块：跳过 / 边界 / 原子 / 正文四条路。

        参数：
            block: 结构块（``content_type`` / ``section_path`` 已由 P1.1/P1.2 标好）。
        返回：无（副作用：可能开/闭 parent、往 ``self._groups`` 追加组）。
        说明：空白块直接忽略；图片块只记 info、不产片段；标题只作 parent 边界。
        """
        strategy = strategy_for(block.content_type)
        if strategy is Strategy.SKIP:
            # 图片不产片段（等 OCR）；记一条 info，界面上能看到"跳过了图片"
            self.sink.add(
                "image_skipped",
                Severity.INFO,
                "文档含图片，暂未 OCR（未索引图片内容）",
            )
            return
        if not block.text.strip():
            # 纯空白块（分隔空行/页眉页脚残留）：没有可索引内容，也不该开 parent
            return
        if strategy is Strategy.BOUNDARY:
            # 判标题只看 content_type（解析层已统一标成 heading）。
            # 文档大标题（level 1 且 section_path 为空）只作元数据，不当 parent 边界。
            if block.level == 1 and not block.section_path:
                return
            level = block.level if block.level is not None else 1
            if self._parent_level is None:
                # 第一个出现的标题层级 = 父块层级（通常 H2；无大标题的文档可能是 H1）
                self._parent_level = level
            # **父块只在"顶层章节"处断开**：H3/H4 并进当前父块（动态父块），
            # 否则每个小节都成一个父块，父块就退化成"子块 + 一行标题"。
            if level <= self._parent_level:
                self._close_parent()
            while self._pending and (self._pending[-1].level or 0) >= level:
                dropped = self._pending.pop()
                if not self._pending_used:
                    # 被同级/更浅标题顶掉、且没等到正文 → 空节：记下来，不静默
                    self.sink.add(
                        "heading_without_body",
                        Severity.INFO,
                        "有标题始终没有等到正文，未产出片段",
                        detail=dropped.text[:80],
                    )
            self._pending.append(block)
            self._pending_used = False
            return
        if strategy in (Strategy.ROW_SPLIT, Strategy.ATOMIC_LINES):
            # 原子块不强行切断章节父块：若当前已有父块（同一 H2 章节），
            # 它的行/代码片段直接并进这个父块（父块 = 整章证据）；
            # 只有在没有父块时（标题后直接跟表格）才单独成组。
            self._emit_atomic(block)
            return
        self._emit_text(block)

    # ---- 内部：正文块 ----

    def _prefix_cost(self, block: Block) -> int:
        """这个块所在的章节标题，在预算里要占多少 token（选项 1B 的关键）。

        参数：
            block: 当前块（用它的 ``section_path`` 取最内层标题）。
        返回：标题前缀 + 分隔符的 token 数；不拼标题或无标题时返回 0。
        说明：开销要算**标题 + 分隔符**，因为嵌入时送进去的就是这两段拼起来的文本。
        """
        if not self._prepend_section:
            return 0
        from app.core.chunk.index_text import SEPARATOR, section_prefix

        # 标题路径**从 H1 起**：文档大标题也是前缀的一部分，预算要算进去
        path = ([self._doc_title] if self._doc_title else []) + list(block.section_path)
        prefix = section_prefix(path)
        if not prefix:
            return 0
        return self._count(prefix + SEPARATOR)

    def _budget_for(self, block: Block) -> int:
        """这一块的**正文**可用预算 = 嵌入预算 − 标题开销。

        为什么这么算（选项 1B）：标题要进向量，就得先从预算里扣掉它，
        否则拼上去必然超 `pad_to`——而超了就是**从尾部静默截断**（P0.2 实测）。
        先前是"先按正文切、再把标题塞进去、塞不下就砍标题"，那是静默丢语境。

        参数：
            block: 当前块（标题开销按它的 ``section_path`` 算）。
        返回：正文可用 token 数，**至少为 1**（极端长标题也不把预算算成 0/负数）。
        """
        return max(1, self._embed_budget - self._prefix_cost(block))

    def _index_length(self, block: Block, text: str) -> int:
        """这段文本**进向量时**的长度（含标题）——所有预算判断都用它。

        参数：
            block: 当前块（提供标题链与 ``prepend_section`` 口径）。
            text: 这段正文（**不含**标题前缀）。
        返回：``count(text) + 标题开销``，即真正送进嵌入的 token 数。
        """
        return self._count(text) + self._prefix_cost(block)

    def _emit_text(self, block: Block) -> None:
        """句级贪心分组：装满预算就封口，**重叠优先于装满**。

        为什么要"重叠优先"（实测踩过）：如果先把当前块塞满、再看重叠塞不塞得下，
        重叠会因为超预算被丢掉——于是"边界句两边都在"这条保护时灵时不灵。
        正确做法是封口时**宁可挤掉本块最后一句**，也要给重叠腾出位置：
        新块 = 上一块末尾整句（重叠）+ 本句，剩下的顺延到下一块。

        参数：
            block: 正文块（``text`` 与定位来源；``_pieces`` 已把它切成片段）。
        返回：无（副作用：向当前 parent 追加 child，必要时先封口一个 parent）。
        """
        pieces = self._pieces(block)
        if not pieces:
            return
        # 同一个块内标题开销固定，预算只算一次（后面每片都复用它）
        budget = self._budget_for(block)
        current: list[_Piece] = []
        overlap_count = 0
        for piece in pieces:
            if not current:
                # 空块用第一片开头：后续才能按 [current[0].start, piece.end) 判断整段
                current = [piece]
                continue
            # 按**拼起来的整段文本**计数，不拿各片 token 相加（计数不可加，
            # 估算取整/分词器边界合并会让累加值系统性偏小）
            if self._count(block.text[current[0].start : piece.end]) <= budget:
                current.append(piece)
                continue
            self._emit_child(block, current, overlap_count=overlap_count)
            tail = self._overlap_tail(block, current)
            # 重叠优先：重叠 + 本句放得下才带；放不下就宁可不要重叠，
            # 也不能让新块一出生就超预算（超了嵌入时会被截尾）
            if tail and self._count(block.text[tail[0].start : piece.end]) <= budget:
                current, overlap_count = [*tail, piece], len(tail)
            else:
                current, overlap_count = [piece], 0
        if current:
            # **软下限**（选项 2A）：最后一块太小就并回上一块——但**只在同一个块内**，
            # 绝不跨段落/跨块合并（空行是作者给的语义边界，那是我们明确不碰的）。
            if (
                len(pieces) > len(current)
                and self._count(block.text[current[0].start : current[-1].end])
                < self._min_child
            ):
                merged = self._try_merge_tail(block, pieces, current)
                if merged:
                    return
            self._emit_child(block, current, overlap_count=overlap_count)

    def _try_merge_tail(self, block: Block, pieces: list[_Piece], current: list[_Piece]) -> bool:
        """把太小的尾块并回前一块（并得下才并）。成功返回 True。

        参数：
            block: 当前块（区间 → 文本/定位）。
            pieces: 本块的全部片段（用来找尾块前的那一片）。
            current: 尚未发出的尾块片段（最后一条 child）。
        返回：合并成功 ``True``；没有可并的前一块，或并了会超预算时 ``False``。
        说明：只在**同一块内**合并（``pieces`` 都是这一块的），绝不跨段落/跨块——
            空行是作者给的语义边界，跨过去就把两段话粘成一段了。
        """
        start = current[0].start
        # 当前尾块之前的那一片 = 上一条 child 的末尾（pieces 内部连续，不跨块）
        remaining = [piece for piece in pieces if piece.end <= start]
        if not remaining or self._last_child_start is None:
            return False
        end = current[-1].end
        # **从上一条 child 的起点**合并（不是最后一片的起点）：后者只覆盖尾块，
        # 会把前一条 child 的前半截整段丢掉（实测：801 字日志只剩 197 字尾块）。
        if self._count(block.text[self._last_child_start : end]) > self._budget_for(block):
            return False  # 并进来会超预算 → 宁可留一个短尾块
        self._replace_last_child(block, self._last_child_start, end)
        return True

    def _replace_last_child(self, block: Block, start: int, end: int) -> None:
        """把当前 parent 的最后一条 child 换成 ``[start, end)`` 这一段（合并尾块用）。

        参数：
            block: 当前块（文本与定位来源）。
            start / end: 合并后的块内字符区间。
        返回：无。
        说明：``hard_split`` 沿用被替换的那条记录——合并不会让硬切"变成"软切，
            引用时的如实说明必须保留；parent 正文不动（两段原文本来就已经在里面）。
        """
        parent = self._open
        if parent is None or not parent.children:
            return
        record = self._record(block, start, end, hard=parent.children[-1].hard_split)
        parent.children[-1] = record

    def _emit_child(self, block: Block, pieces: list[_Piece], *, overlap_count: int) -> None:
        """把若干片段并成一条 child，并挂到当前 parent 上。

        参数：
            block: 当前块（文本与定位来源）。
            pieces: 组成这条 child 的片段（开头可能包含上一块的重叠片段）。
            overlap_count: 开头有几个片段是"上一块的重叠"，**不算进 parent 正文**。
        返回：无（副作用：往 parent 追加 child；必要时先封口、再开新 parent）。
        说明：parent 是送给模型的证据，必须守住自己的预算；child 是检索单位，
            允许带重叠（边界句两边都在），但重叠只是冗余、不能重复算成证据。
        """
        start = pieces[0].start
        end = pieces[-1].end
        record = self._record(block, start, end, hard=any(piece.hard for piece in pieces))
        # 进 parent 正文的是**非重叠的新增原文**（重叠只是给检索的冗余，不该算进证据）
        source = block.text[pieces[overlap_count].start : end]
        tokens = self._count(source)
        parent = self._ensure_parent(block)
        # **加之前**预判：parent 是送给模型的证据，必须守住自己的预算。
        # 先前写成"加完再看"，结果 parent 能超预算一整个 child；块正好在此时结束时，
        # 那个超出的 child 就永远留在超预算的 parent 里（实测：60 的预算装出 82）。
        # **按拼好的文本重新计数**（不拿累加值比）：token 计数不可加——估算有取整、
        # 真分词器会在拼接边界合并 token，累加值会系统性偏小。
        if parent.children and self._count(parent.text + "\n" + source) > self._parent_size:
            tail = self._parent_tail(parent.text)
            self._close_parent()
            parent = self._ensure_parent(block)
            if tail:
                parent.text_parts.append(tail)
                parent.tokens += self._count(tail)
                logger.debug("超长节再分 parent（重叠 {} 字符）", len(tail))
        # 同一 parent 内：新块或上一条很短、且同类（正文/署名）→ 并进上一条，
        # 避免产出几十字的碎块（公文/论文常见：作者行、单位行、只有一句的过渡段）。
        # 只在**不超 child 预算**时合并。
        last = parent.children[-1] if parent.children else None
        if (
            last is not None
            and last.content_type == record.content_type
            and record.content_type in (TYPE_TEXT, TYPE_BYLINE)
            and last.section_path == record.section_path
            and source.strip()
            and (
                self._count(record.text) < self._min_child
                or self._count(last.text) < self._min_child
            )
            and self._count(last.text + "\n" + source) <= self._child_size
        ):
            parent.children[-1] = replace(
                last,
                # 用**非重叠**的 source：record.text 开头可能是上一块的重叠句，
                # 直接拼会让边界句在 child 里出现两次
                text=last.text + "\n" + source,
                line_to=record.line_to if record.line_to is not None else last.line_to,
                page_to=record.page_to if record.page_to is not None else last.page_to,
                # 任一硬切过就保留硬切标记（合并不能让硬切"变成"软切）
                hard_split=last.hard_split or record.hard_split,
            )
            parent.text_parts.append(source)
            parent.tokens += tokens
            return
        # 正常路径：child 挂上去，非重叠原文并进 parent 正文（tokens 只是近似值）
        parent.add(record, source, tokens=tokens)
        # 记住这条 child 的起点：后面合并短尾块时要从这里开始，不能从尾块前一片开始
        self._last_child_start = start
        # **守门**：向量输入（标题 + 正文）绝不允许超嵌入预算——超了就是尾部被静默截断。
        # 按选项 1B，预算是逐块算进去的，所以这里正常不会触发；触发就是切分逻辑有 bug，
        # 必须留下痕迹（而不是像早先那样悄悄把标题砍掉）。
        length = self._index_length(block, record.text)
        if length > self._embed_budget:
            # **必须进 sink**：任务层"ERROR 即失败"依赖它；只写日志的话
            # 真发生尾部截断时用户看不到、任务也不会失败。
            self.sink.add(
                "embed_budget_exceeded",
                Severity.ERROR,
                f"切片超出嵌入预算（{length} > {self._embed_budget}），尾部会被截断",
                detail=record.text[:80],
            )

    def _overlap_tail(self, block: Block, pieces: list[_Piece]) -> list[_Piece]:
        """取这些片段末尾凑够 ``child_overlap`` 的**整句**（返回片段列表）。

        参数：
            block: 当前块（文本来源）。
            pieces: 已组成 child 的片段；用它们的整体区间重新按句切。
        返回：末尾若干整句的片段（升序）；重叠关闭、只有一句、或凑不够时返回 ``[]``。
        说明：重叠必须**整句**——从半句开始的重叠引用时读不通；从末尾往前累加，
            够 ``child_overlap`` 即停。只有一个整句时重叠它等于把整块重复一遍，所以不做。
        """
        if self._child_overlap <= 0 or not pieces:
            return []
        # 在"这个 child 的区间"里重新切句，并把句子坐标偏移回块坐标（+ pieces[0].start）
        spans = [
            (span_start + pieces[0].start, span_end + pieces[0].start)
            for span_start, span_end in _sentence_spans(
                block.text[pieces[0].start : pieces[-1].end]
            )
        ]
        if len(spans) < 2:
            return []  # 只有一个整句时重叠它等于把整块重复一遍，没意义
        chosen: list[_Piece] = []
        total = 0
        # 从末尾往前取整句：越靠近边界的话越可能被下一块需要
        for span_start, span_end in reversed(spans):
            candidate = _Piece(span_start, span_end)
            chosen.insert(0, candidate)
            # 按"候选首句 → child 末尾"的拼接文本计数（不累加各句 token：计数不可加）
            total = self._count(block.text[candidate.start : pieces[-1].end])
            if total >= self._child_overlap:
                break
        return chosen

    def _pieces(self, block: Block) -> list[_Piece]:
        """把块切成片段：**整句优先 → 软边界（标点+连接词）→ 硬切**。

        参数：
            block: 正文块。
        返回：片段列表（升序、互不重叠）；空块返回空列表。
        规则（§4.3）：
        - 句子放得下 → 整句一片，不切；
        - 放不下 → 在软边界切（句读标点 + 连接词，段首），对每段**递归**；
        - 没有任何软边界 → 按 token 预算硬切（并记一条诊断，不静默）。
        """
        text = block.text
        budget = self._budget_for(block)
        raw: list[_Piece] = []
        # 整句放得下时 _cut 直接返回，不会进入任何降级逻辑（最常见路径）
        for start, end in _sentence_spans(text):
            raw.extend(self._cut(block, text, start, end, budget))
        return raw

    def _cut(self, block: Block, text: str, start: int, end: int, budget: int) -> list[_Piece]:
        """递归切分一段 ``[start, end)``：放得下就停，否则降一级。

        参数：
            block: 当前块（预算/标题口径与定位来源）。
            text: 块文本（逐层传下去，避免每层重新取）。
            start / end: 当前要切的区间（左闭右开）。
            budget: 这一块的正文预算（已扣标题开销）。
        返回：至少一个片段的列表；**不抛异常**（语义兜底内部会吞异常并回退）。
        降级顺序：放得下 → **软边界递归**（能切就不花 embedding）→ 语义兜底（可选，
        有单文档次数上限）→ 硬切 + 告警。

        为什么软边界要排在语义兜底前面：对话体/小说里长对白被引号保护，整段看起来
        "没有句末标点"，但里面全是逗号/顿号。先按软边界切，既快又不会把一句对白
        交给 ONNX；只有真的连软边界都没有（无标点长段落）才值得用语义阶梯。
        """
        if self._count(text[start:end]) <= budget:
            return [_Piece(start, end)]

        # ① 软边界优先：逗号/顿号/连接词能切就切（引号内长对白不必跑 embedding）
        spans = soft_spans(text, start, end)
        if len(spans) == 1:
            # 整段被**引号**保护住（长篇对白）：再试一次"忽略引号"的软切。
            # 括号仍然受保护；只放开引号，否则几百字对白会被当成一句去跑 ONNX。
            spans = soft_spans(text, start, end, respect_quotes=False)
        spans = _merge_atom_spans(text, spans)
        if len(spans) > 1:
            pieces: list[_Piece] = []
            # 软边界只是"更细的候选"：每段仍可能超预算，所以继续递归（不是一次切完）
            for part_start, part_end in spans:
                pieces.extend(self._cut(block, text, part_start, part_end, budget))
            return pieces

        # ② 真的没有软边界 → 语义阶梯；单文档调用次数有上限（防 embedding 风暴）
        if self._semantic is not None:
            if self._semantic_calls >= SEMANTIC_CALLS_PER_DOC:
                if not self._semantic_cap_logged:
                    self._semantic_cap_logged = True
                    self.sink.add(
                        "semantic_cap_reached",
                        Severity.WARNING,
                        f"语义兜底单文档调用已达上限（{SEMANTIC_CALLS_PER_DOC} 次），后续长段落改为硬切",
                        detail=text[start : start + 80],
                    )
            else:
                self._semantic_calls += 1
                semantic_pieces = self._semantic_pieces(text, start, end, budget)
                if semantic_pieces:
                    return semantic_pieces

        # ③ 没有任何软边界：只能硬切（记为告警，界面可见）
        self.sink.add(
            "hard_cut_used",
            Severity.WARNING,
            "存在没有可用标点的超长段落，已按字符硬切",
            detail=text[start:end][:80],
            level="char",
            loc=f"page={block.page} line={block.line_from}",
            budget=budget,
            actual=self._count(text[start:end]),
        )
        return self._hard_fit(text, start, end, budget)

    def _semantic_pieces(self, text: str, start: int, end: int, budget: int) -> list[_Piece]:
        """语义兜底：把超长无标点段落交给语义切分器，拿回若干片段的**文本**。

        参数：
            text: 块文本。
            start / end: 要兜底的区间（超预算、且没有可用软边界）。
            budget: 目标块大小（传给语义切分器）。
        返回：按原文顺序排好的片段；定位不上/切分失败/没切出多段时返回 ``[]``
            （调用方据此退回标点/硬切）。
        异常：**不向外抛**——语义兜底失败不该让整份导入失败，只记 warning。
        说明：片段文本按顺序在原文里定位（找不到就整体放弃，**绝不猜位置**），
            定位决定引用跳转，猜错比不兜底更糟。
        """
        assert self._semantic is not None
        try:
            parts = self._semantic(text[start:end], budget)
        except Exception as exc:  # 兜底失败不该让导入失败
            logger.warning("语义切分失败，退回标点/硬切：{}", exc)
            return []
        if not parts:
            return []
        pieces: list[_Piece] = []
        cursor = start
        for part in parts:
            found = text.find(part, cursor, end)
            if found < 0:
                return []  # 定位不上就整体放弃（宁可不兜底，也不能给错位置）
            pieces.append(_Piece(found, found + len(part)))
            cursor = found + len(part)
        return pieces

    def _hard_fit(self, text: str, start: int, end: int, budget: int) -> list[_Piece]:
        """没有标点可用的兜底：按预算硬切（并标记 ``hard``——引用时要如实说明）。

        参数：
            text: 块文本。
            start / end: 要切的区间（没有任何软边界）。
            budget: 正文预算。
        返回：若干 ``hard=True`` 的片段，覆盖整个区间、每片都 ≤ budget。
        说明：每刀的长度用**二分**找"能塞进预算的最长前缀"：token 数随长度单调不减，
            所以二分既精确又不依赖"1 token = 0.69 字符"这个估算（真分词器口径下同样准）。
        """
        pieces: list[_Piece] = []
        cursor = start
        while cursor < end:
            step = self._fit_length(text, cursor, end, budget)
            pieces.append(_Piece(cursor, cursor + step, hard=True))
            cursor += step
        return pieces

    def _fit_length(self, text: str, start: int, end: int, budget: int) -> int:
        """``[start, end)`` 里**能塞进预算的最长长度**（至少 1）。

        参数：
            text: 块文本。
            start / end: 搜索区间（调用方保证 ``start < end``）。
            budget: 正文预算。
        返回：长度（``1 ≤ 返回值 ≤ end - start``）；即使 1 个字符也超预算也返回 1，
            保证硬切循环一定前进（否则会死循环）。
        """
        low, high = 1, end - start
        best = 1
        while low <= high:
            middle = (low + high) // 2
            if self._count(text[start : start + middle]) <= budget:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        # **硬切回退**：宁可少塞 ~40 字，也要切在最近的标点/空白后，不把词/句子劈两半。
        # 只有"预算边界前不远处就有个标点/空白"时才回退；整段都没有就保持原样（best）。
        segment = text[start : start + best]
        backoff = _last_break_before_end(segment)
        return backoff if backoff > 0 else best

    def _record(self, block: Block, start: int, end: int, *, hard: bool) -> ChunkRecord:
        """按块内字符区间造一条记录（行号/bbox 都按区间映射）。

        参数：
            block: 来源块。
            start / end: 块内字符区间（左闭右开）。
            hard: 是否硬切（写入 ``ChunkRecord.hard_split``）。
        返回：不可变的 :class:`ChunkRecord`。
        说明：``text`` 去两端空白只是兜底；定位仍按**原区间**算
            （正常路径的区间已由 ``_trim`` 收边，两者一致）。
        """
        text = block.text[start:end].strip()
        line_from, line_to = _line_span(block, start, end)
        return ChunkRecord(
            text=text,
            content_type=block.content_type,
            section_path=tuple(block.section_path),
            level=block.level,
            page=block.page,
            page_to=block.page_to,
            line_from=line_from,
            line_to=line_to,
            table_ref=block.table_ref,
            row_from=block.row_from,
            row_to=block.row_to,
            col_from=block.col_from,
            col_to=block.col_to,
            header=tuple(block.header),
            coords=_coords_span(block, start, end),
            para_index=block.para_index,
            hard_split=hard,
        )

    # ---- 内部：原子块（表格 / 代码） ----

    def _emit_atomic(self, block: Block) -> None:
        """表格/代码：整块成 parent；**超预算时按整行切**（表格重复表头）。

        参数：
            block: 表格/代码块（``content_type`` 决定是否重复表头）。
        返回：无（副作用：直接向 ``self._groups`` 追加组，**不经过** ``_OpenParent``）。
        说明：原子块没有句级结构，只能按整行切；单行仍超预算时才按字符硬切并告警。
            表头在每段重复（见 ``_atomic_parts``），否则从第二段起的数字没有列名。
        """
        lines = block.text.split("\n")
        if (
            block.content_type == TYPE_TABLE
            and block.header
            and not any(": " in line for line in lines if line.strip())
        ):
            self._emit_plain_table(block)
            return
        # xlsx 的行块（``列名: 值``）：走表级规则——一行一个 parent，完整行是父块；
        # 行放不下才按列拆 child。这样命中任何子块，回填父块都能拿到完整一行。
        if (
            block.content_type == TYPE_TABLE
            and block.header
            and any(": " in line for line in lines if line.strip())
        ):
            self._emit_table_rows(block)
            return
        parts = self._atomic_parts(block, lines)
        # 原子块直接攒批：攒到 parent 预算就封一组，避免一个超长表格/代码文件
        # 变成"一个巨型 parent"；分批只看 token 粗值，真正的切分行边界已由 parts 保证
        batch: list[ChunkRecord] = []
        tokens = 0
        for part_lines, row_from, row_to in parts:
            text = "\n".join(part_lines)
            budget = self._budget_for(block)
            hard = False
            if self._count(text) > budget:
                # 超宽表格行：按**列**切，表头也必须同步切（否则每段都带整行 40 列表头，
                # 但数据只有几列，表头与值错位、列号也不对）。拆不了才字符硬切。
                table_pieces = self._split_wide_table_part(block, text, budget)
                if table_pieces:
                    pieces = table_pieces
                else:
                    # 单行/单格就超预算（超长代码行 / 超宽单元格）：按字符硬切，不静默
                    self.sink.add(
                        "atomic_line_over_budget",
                        Severity.WARNING,
                        "表格/代码存在超长单行，已按字符硬切",
                        detail=text[:80],
                        level="char",
                        loc=f"table={block.table_ref} row={row_from}",
                        budget=budget,
                        actual=self._count(text),
                        atom_type="table" if block.content_type == TYPE_TABLE else "code",
                    )
                    pieces = [
                        (text[piece.start : piece.end], block.col_from, block.col_to, tuple(block.header))
                        for piece in self._hard_fit(text, 0, len(text), budget)
                    ]
                    hard = True
            else:
                pieces = [(text, block.col_from, block.col_to, tuple(block.header))]
            for piece_text, col_from, col_to, header in pieces:
                record = ChunkRecord(
                    text=piece_text,
                    content_type=block.content_type,
                    role="table_row" if block.content_type == TYPE_TABLE else "code",
                    fetch_mode=1,
                    section_path=tuple(block.section_path),
                    level=block.level,
                    page=block.page,
                    page_to=block.page_to,
                    line_from=block.line_from,
                    line_to=block.line_to,
                    table_ref=block.table_ref,
                    row_from=row_from,
                    row_to=row_to,
                    col_from=col_from,
                    col_to=col_to,
                    header=header,
                    coords=block.coords,
                    # 一个 part 被硬切成多段时，每一段都是从行中间切开的（结尾不完整）
                    hard_split=hard,
                )
                piece_tokens = self._count(piece_text)
                if self._open is not None:
                    # 章节父块已经开着：表格/代码片段并进去（父块 = 整章证据）
                    self._flush_pending_into(self._open)
                    self._append_to_open_parent(block, record, piece_tokens)
                    continue
                # **先判后加**：加了再判会让 parent 超出 parent_size 一个 child
                if batch and tokens + piece_tokens > self._parent_size:
                    self._emit_atomic_group(block, batch)
                    batch, tokens = [], 0
                batch.append(record)
                tokens += piece_tokens
        if batch:
            self._emit_atomic_group(block, batch)

    def _with_table_title(self, block: Block, path: tuple[str, ...]) -> tuple[str, ...]:
        """把显式表格标题追加到章节链末尾（不覆盖章节）。"""
        title = str(block.table_title or "").strip()
        if not title:
            return path
        if path and path[-1] == title:
            return path
        return (*path, title)

    def _emit_plain_table(self, block: Block) -> None:
        """DOCX/PDF/PPTX/MD/CSV 的普通表格：摘要 parent + 行 child（`列名: 值`）。

        行放得下 → `fetch_mode=0` 的 table_row；
        行超宽 → 拆列组，`fetch_mode=2`，同 `row_uid`，按 `segment_index` 拼回。
        """
        lines = [line for line in block.text.split("\n") if line.strip()]
        if not lines:
            return
        header = tuple(str(cell).strip() for cell in block.header)
        first_cells = [cell.strip() for cell in lines[0].split(" | ")]
        rows = lines
        header_skipped = False
        if header and len(first_cells) >= 1:
            matches = sum(1 for got, want in zip(first_cells, header, strict=False) if got == want)
            if matches >= max(1, len(header) // 2):
                rows = lines[1:]
                header_skipped = True
        columns = header or tuple(first_cells)
        if not columns:
            return

        ref = str(block.table_ref or "表格")
        self._table_seq += 1
        table_uid = f"{ref}#{self._table_seq}"
        path = self._with_table_title(block, tuple(block.section_path))
        title = path[-1] if path else "表格"
        level = block.level if block.level is not None else 1
        cols = " | ".join(columns)
        row_range = (
            f"第 {block.row_from} 行" if block.row_from is not None else "行范围未知"
        )
        summary_text = (
            f"{title}\n表摘要：{ref}\n范围：{row_range} · {len(columns)} 列\n列：{cols}"
        )
        parent = ChunkRecord(
            text=summary_text,
            is_parent=True,
            content_type=TYPE_TABLE,
            role="table_parent",
            fetch_mode=0,
            table_uid=table_uid,
            section_path=path,
            level=level,
            page=block.page,
            page_to=block.page_to,
            table_ref=block.table_ref,
            row_from=block.row_from,
            row_to=block.row_to,
            col_from=block.col_from,
            col_to=block.col_to,
            header=columns,
            merged_ranges=tuple(block.merged_ranges),
        )
        children_list: list[ChunkRecord] = [replace(parent, is_parent=False)]

        budget = self._budget_for(block)
        base = int(block.row_from or 1) + (1 if header_skipped else 0)
        for offset, line in enumerate(rows):
            row_no = base + offset
            row_uid = f"{table_uid}#r{row_no}"
            cells = [cell.strip() for cell in line.split(" | ")]
            pairs = [
                f"{columns[i] if i < len(columns) else f'列{i + 1}'}: {cell}"
                for i, cell in enumerate(cells)
            ]
            full = " | ".join(pairs)
            if self._count(full) <= budget:
                children_list.append(
                    ChunkRecord(
                        text=full,
                        content_type=TYPE_TABLE,
                        role="table_row",
                        fetch_mode=0,
                        table_uid=table_uid,
                        row_uid=row_uid,
                        row_no=row_no,
                        segment_index=0,
                        segment_count=1,
                        section_path=path,
                        level=level,
                        table_ref=block.table_ref,
                        row_from=row_no,
                        row_to=row_no,
                        col_from=block.col_from,
                        col_to=block.col_to,
                        header=columns,
                    )
                )
                continue

            groups: list[list[str]] = []
            current: list[str] = []
            for pair in pairs:
                candidate = " | ".join([*current, pair])
                if current and self._count(candidate) > budget:
                    groups.append(current)
                    current = [pair]
                else:
                    current.append(pair)
            if current:
                groups.append(current)
            segment_children = tuple(
                ChunkRecord(
                    text=" | ".join(group),
                    content_type=TYPE_TABLE,
                    role="table_row_segment",
                    fetch_mode=2,
                    table_uid=table_uid,
                    row_uid=row_uid,
                    row_no=row_no,
                    segment_index=index,
                    segment_count=len(groups),
                    section_path=path,
                    level=level,
                    table_ref=block.table_ref,
                    row_from=row_no,
                    row_to=row_no,
                    col_from=block.col_from,
                    col_to=block.col_to,
                    header=columns,
                )
                for index, group in enumerate(groups)
            )
            children_list.extend(segment_children)
        self._groups.append(ChunkGroup(parent=parent, children=tuple(children_list)))

    def _emit_table_rows(self, block: Block) -> None:
        """xlsx 表：**普通行共享表级摘要 parent；超宽行单独建 parent（摘要+完整行）**。

        规则（定稿）：
        - 每张表一个 parent = 标题 + 全局摘要，普通行 child 都指向它；
        - 行放得下 → child = 标题 + 完整一行；
        - 行放不下 → 为这行新建 parent = 全局摘要 + 完整一行，children = 列片段；
        - 命中任意列片段，回填 parent 就是"摘要 + 完整一行"。
        """
        lines = [line for line in block.text.split("\n") if line.strip()]
        if not lines:
            return
        ref = str(block.table_ref or "表格")
        self._table_seq += 1
        table_uid = f"{ref}#{self._table_seq}"
        path = tuple(block.section_path)
        if not path:
            heads, self._pending = self._pending, []
            self._pending_used = True
            if heads:
                path = tuple(heads[-1].section_path) or (heads[-1].text.strip(),)
            elif ref.startswith("sheet:"):
                path = (ref[len("sheet:") :].split("#", 1)[0],)
            elif self._doc_title:
                path = (self._doc_title,)
        path = self._with_table_title(block, path)
        title = path[-1] if path else "表格"
        level = block.level if block.level is not None else 1
        columns = tuple(block.header)
        if block.row_from is not None and block.row_to is not None:
            row_range = (
                f"第 {block.row_from} 行"
                if block.row_from == block.row_to
                else f"第 {block.row_from}–{block.row_to} 行"
            )
        else:
            row_range = "行范围未知"
        cols = " | ".join(columns) if columns else "（未识别列名）"
        summary_text = (
            f"{title}\n表摘要：{ref}\n范围：{row_range} · {len(columns)} 列\n列：{cols}"
        )
        table_parent = ChunkRecord(
            text=summary_text,
            is_parent=True,
            content_type=TYPE_TABLE,
            role="table_parent",
            fetch_mode=0,
            table_uid=table_uid,
            section_path=path,
            level=level,
            page=block.page,
            page_to=block.page_to,
            table_ref=block.table_ref,
            row_from=block.row_from,
            row_to=block.row_to,
            col_from=block.col_from,
            col_to=block.col_to,
            header=columns,
            merged_ranges=tuple(block.merged_ranges),
        )
        # 摘要 child：每张表只嵌一次，供"这张表有哪些列/多少行"召回
        table_children: list[ChunkRecord] = []
        if ref not in self._table_summary_done:
            self._table_summary_done.add(ref)
            table_children.append(replace(table_parent, is_parent=False))

        budget = self._budget_for(block)
        piece_budget = budget
        base = int(block.row_from or 1)
        for offset, line in enumerate(lines):
            row_no = base + offset
            row_uid = f"{table_uid}#r{row_no}"
            row_text = line
            if self._count(row_text) <= budget:
                child = ChunkRecord(
                    text=row_text,
                    content_type=TYPE_TABLE,
                    role="table_row",
                    fetch_mode=0,
                    table_uid=table_uid,
                    row_uid=row_uid,
                    row_no=row_no,
                    segment_index=0,
                    segment_count=1,
                    section_path=path,
                    level=level,
                    table_ref=block.table_ref,
                    row_from=row_no,
                    row_to=row_no,
                    col_from=block.col_from,
                    col_to=block.col_to,
                    header=columns,
                )
                table_children.append(child)
                continue

            piece_budget = budget
            pieces = self._split_labeled_pairs(block, line, piece_budget)
            children_list: list[ChunkRecord] = []
            digest = self._row_key_digest(block, line, piece_budget)
            if digest is not None:
                digest_text, digest_from, digest_to, digest_header = digest
                children_list.append(
                    ChunkRecord(
                        text=digest_text,
                        content_type=TYPE_TABLE,
                        section_path=path,
                        level=level,
                        table_ref=block.table_ref,
                        row_from=row_no,
                        row_to=row_no,
                        col_from=digest_from,
                        col_to=digest_to,
                        header=digest_header,
                    )
                )
            if pieces:
                children_list.extend(
                    ChunkRecord(
                        text=text,
                        content_type=TYPE_TABLE,
                        section_path=path,
                        level=level,
                        table_ref=block.table_ref,
                        row_from=row_no,
                        row_to=row_no,
                        col_from=col_from,
                        col_to=col_to,
                        header=header,
                    )
                    for text, col_from, col_to, header in pieces
                )
            else:
                children_list.extend(
                    ChunkRecord(
                        text=line[piece.start : piece.end],
                        content_type=TYPE_TABLE,
                        section_path=path,
                        level=level,
                        table_ref=block.table_ref,
                        row_from=row_no,
                        row_to=row_no,
                        col_from=block.col_from,
                        col_to=block.col_to,
                        header=columns,
                        hard_split=True,
                    )
                    for piece in self._hard_fit(line, 0, len(line), piece_budget)
                )
            children = tuple(
                replace(
                    child,
                    role="table_row_segment",
                    fetch_mode=2,
                    table_uid=table_uid,
                    row_uid=row_uid,
                    row_no=row_no,
                    segment_index=index,
                    segment_count=len(children_list),
                )
                for index, child in enumerate(children_list)
            )
            # 超宽行：所有列组都指向**表格摘要 parent**；整行由检索层按 row_uid 拼回
            table_children.extend(children)
        self._groups.append(ChunkGroup(parent=table_parent, children=tuple(table_children)))

    def _append_to_open_parent(
        self, block: Block, record: ChunkRecord, tokens: int
    ) -> None:
        """把一个原子块 child 并进**当前开着的父块**（守 parent 预算）。"""
        parent = self._open
        if parent is None:
            return
        source = record.text
        if parent.children and self._count(parent.text + "\n" + source) > self._parent_size:
            tail = self._parent_tail(parent.text)
            self._close_parent()
            parent = self._ensure_parent(block)
            if tail:
                parent.text_parts.append(tail)
                parent.tokens += self._count(tail)
        parent.add(record, source, tokens=tokens)

    def _emit_atomic_group(self, block: Block, records: list[ChunkRecord]) -> None:
        """把一个批次封成 ``ChunkGroup``（原子块的 parent 直接由 child 拼出来）。

        参数：
            block: 原子块（提供类型/结构/定位/表格引用）。
            records: 本批 child 记录（非空；按行顺序）。
        返回：无（副作用：向 ``self._groups`` 追加一组）。
        说明：不走 ``_OpenParent``——表格/代码自成一体，不需要标题合并、短块合并；
            parent 的 ``row_from/row_to`` 取首尾 child 的行号，引用能定位到整段表格。
        """
        # 消耗挂起标题（避免被下一个块误用），但**不写进父块正文**：
        # 父块正文只放内容，标题由 section_path 元数据单独提供。
        heads, self._pending = self._pending, []
        self._pending_used = True
        if self._parent_path:
            section_path = self._parent_path
            level = self._parent_level
        elif heads:
            # 标题后直接跟表格（还没有正文父块）：用该标题当父块的章节元数据
            top = heads[0]
            section_path = tuple(top.section_path) or (top.text.strip(),)
            level = top.level
        else:
            section_path = tuple(block.section_path)
            level = block.level
        parent_text = "\n".join(record.text for record in records)
        parent = ChunkRecord(
            text=parent_text,
            is_parent=True,
            content_type=block.content_type,
            role="table_parent" if block.content_type == TYPE_TABLE else "section_parent",
            fetch_mode=0,
            # 原子块也归到**当前顶层章节**（H2）：表格/代码是该章的一部分，
            # 不把父块标成最内层小节（否则同一章会出现多个"章节"不同的父块）。
            section_path=section_path,
            level=level,
            page=block.page,
            page_to=block.page_to,
            line_from=block.line_from,
            line_to=block.line_to,
            table_ref=block.table_ref,
            row_from=records[0].row_from,
            row_to=records[-1].row_to,
            col_from=block.col_from,
            col_to=block.col_to,
            header=tuple(block.header),
            coords=block.coords,
        )
        self._groups.append(ChunkGroup(parent=parent, children=tuple(records)))

    def _atomic_parts(
        self, block: Block, lines: list[str]
    ) -> list[tuple[list[str], int | None, int | None]]:
        """把原子块切成"能塞进 child 预算"的若干段（**整行**，不切半行）。

        参数：
            block: 原子块（预算/表头/行号基准）。
            lines: ``block.text`` 按换行切开的原始行。
        返回：``[(行列表, row_from, row_to), ...]``；每段（含重复表头）尽量 ≤ budget。
        约定：表格**每段重复首行**（表头），否则第 2 段开始的数字是无头数据、
            人和模型都读不懂；代码不重复（没有表头概念）。
        """
        # 空行不参与切分（它们不是数据行），但仍保留原始行下标用于行号映射
        indexed = [(index, line) for index, line in enumerate(lines) if line.strip()]
        if not indexed:
            return []
        # xlsx 行块：一行就是一个 child（哪怕几行合起来仍放得下，也不合并——
        # 否则一张小表会变成"一整块"，行级引用/检索又丢了）。
        if (
            block.content_type == TYPE_TABLE
            and block.header
            and any(": " in line for _index, line in indexed)
        ):
            return [
                ([line], block.row_from + offset if block.row_from is not None else None,
                 block.row_from + offset if block.row_from is not None else None)
                for offset, (_index, line) in enumerate(indexed)
            ]
        whole = "\n".join(line for _index, line in indexed)
        budget = self._budget_for(block)
        header: str | None = None
        if block.content_type == TYPE_TABLE:
            first = indexed[0][1]
            # xlsx 行已经序列化成 ``列名: 值``（带 block.header），没有裸表头行；
            # docx/pptx/csv/md 仍是"首行表头 + 数据行"，继续把首行当表头重复。
            if not (block.header and ": " in first):
                header = first
        if self._count(whole) <= budget:
            row_from = block.row_from
            if header is not None and block.row_from is not None:
                # 表头在块内占一行；引用要指向**数据行**，不是表头行。
                row_from = block.row_from + 1
                if block.row_to is not None and row_from > block.row_to:
                    row_from = block.row_from
            return [([line for _index, line in indexed], row_from, block.row_to)]

        # 超宽的**表格行**不在这里拆：拆行会让每个单元格组都重复表头、片段数翻倍。
        # 只在 `_emit_atomic` 发现"这一段（表头+一行）真超预算"时，才按单元格边界拆，
        # 单格仍超才字符硬切（见 `_split_wide_table_part`）。
        header_tokens = self._count(header) if header else 0
        parts: list[tuple[list[str], int | None, int | None]] = []

        parts: list[tuple[list[str], int | None, int | None]] = []
        current: list[str] = []
        current_indexes: list[int] = []
        for index, line in indexed:
            if header is not None and line == header and index == indexed[0][0]:
                continue  # 表头统一在每段开头补，这里不重复收
            candidate = [*current, line]
            # 候选段 token = 标题前缀 + 本段表头 + 数据行（**按拼好的文本计数**）
            candidate_tokens = header_tokens + self._count("\n".join(candidate))
            if current and candidate_tokens > budget:
                # 放不下当前行：先把已攒的这段封口，再以当前行开新段
                parts.append(self._finish_part(current, current_indexes, header, block))
                current, current_indexes = [line], [index]
            else:
                current.append(line)
                current_indexes.append(index)
        if current:
            parts.append(self._finish_part(current, current_indexes, header, block))
        return parts

    def _split_wide_table_part(
        self, block: Block, text: str, budget: int
    ) -> list[tuple[str, int | None, int | None, tuple[str, ...]]]:
        """把"表头 + 一行超宽数据"按 **列** 拆成若干段（**表头同步拆、列号跟着走**）。

        只在 :meth:`_emit_atomic` 发现这一段真超预算时调用：
        - 拆不动（没有表头/多行数据）→ 返回 ``[]``，交给字符硬切；
        - 单个单元格本身就超预算 → 返回 ``[]``（硬切那一段并记诊断）。

        返回：``[(片段文本, col_from, col_to, 表头子集), ...]``。
        旧实现每段都重复**整行表头**、列号仍报整张表，导致 40 列表头配 3 列数据、
        引用列范围也错；这里必须按同一列下标同时取表头与数据。
        """
        if "\n" not in text:
            # xlsx 的 child 已经是单行 ``列名: 值 | 列名: 值``（没有裸表头）；
            # 超预算时按 `` | `` 列对拆，列名随值走，绝不从一格中间切。
            return self._split_labeled_pairs(block, text, budget)
        if " | " not in text:
            return []
        header, row = text.split("\n", 1)
        if "\n" in row:
            return []
        header_cells = header.split(" | ")
        row_cells = row.split(" | ")
        total = max(len(header_cells), len(row_cells))
        base = int(block.col_from or 1)
        pieces: list[tuple[str, int | None, int | None, tuple[str, ...]]] = []
        start = 0
        while start < total:
            end = start
            cur_header: list[str] = []
            cur_row: list[str] = []
            while end < total:
                h = header_cells[end] if end < len(header_cells) else ""
                r = row_cells[end] if end < len(row_cells) else ""
                candidate = " | ".join([*cur_header, h]) + "\n" + " | ".join([*cur_row, r])
                if cur_header and self._count(candidate) > budget:
                    break
                cur_header.append(h)
                cur_row.append(r)
                end += 1
            if end == start:  # 单列就超预算：不在这里硬切，交给调用方
                return []
            piece = " | ".join(cur_header) + "\n" + " | ".join(cur_row)
            if self._count(piece) > budget:
                return []
            pieces.append((piece, base + start, base + end - 1, tuple(cur_header)))
            start = end
        return pieces if len(pieces) > 1 else []

    def _row_key_digest(
        self, block: Block, line: str, budget: int
    ) -> tuple[str, int | None, int | None, tuple[str, ...]] | None:
        """超宽行的**关键字段摘要**：把编号/名称/金额/凭证等塞进一小段。

        递归按列切会把"科目编号"和"金额"分到两段，问"某科目金额"时两段都不完整。
        关键摘要仍挂在同一行 parent 下，保证组合字段能命中。
        """
        pairs = line.split(" | ")
        hints = ("编号", "代码", "名称", "金额", "凭证", "项", "公司", "利润中心", "日期", "年度", "期间", "科目")
        selected: list[str] = []
        for pair in pairs:
            label = pair.split(": ", 1)[0]
            if not any(hint in label for hint in hints):
                continue
            candidate = " | ".join([*selected, pair])
            if self._count(candidate) <= budget:
                selected.append(pair)
        if not selected or len(selected) == len(pairs):
            return None
        label_to_col: dict[str, int] = {}
        for index, label in enumerate(block.header):
            label_to_col.setdefault(label, int(block.col_from or 1) + index)
        labels = tuple(item.split(": ", 1)[0] for item in selected)
        cols = [label_to_col[label] for label in labels if label in label_to_col]
        return (
            " | ".join(selected),
            min(cols) if cols else block.col_from,
            max(cols) if cols else block.col_to,
            labels,
        )

    def _split_labeled_pairs(
        self, block: Block, text: str, budget: int
    ) -> list[tuple[str, int | None, int | None, tuple[str, ...]]]:
        """拆单行 ``列名: 值 | 列名: 值``：**递归二分列，直到每段 ≤ 预算**。"""
        return self._split_pairs_recursive(block, text, budget)

    def _split_pairs_recursive(
        self, block: Block, text: str, budget: int
    ) -> list[tuple[str, int | None, int | None, tuple[str, ...]]]:
        """递归把一行按列二分；放得下就停，单格仍超才字符硬切。"""
        label_to_col: dict[str, int] = {}
        for index, label in enumerate(block.header):
            label_to_col.setdefault(label, int(block.col_from or 1) + index)

        def col_span(piece: str) -> tuple[int | None, int | None, tuple[str, ...]]:
            labels = tuple(item.split(": ", 1)[0] for item in piece.split(" | "))
            cols = [label_to_col[label] for label in labels if label in label_to_col]
            return (
                min(cols) if cols else block.col_from,
                max(cols) if cols else block.col_to,
                labels,
            )

        if self._count(text) <= budget:
            col_from, col_to, labels = col_span(text)
            return [(text, col_from, col_to, labels)]

        pairs = text.split(" | ")
        if len(pairs) <= 1:
            # **单元格是一个整体**：再长也不按字符切。整格作为一个 child（可能超预算，
            # 嵌入阶段会截断，但 payload/引用仍是完整值；父块也保留完整行）。
            _, col, _ = col_span(text)
            label = pairs[0].split(": ", 1)[0] if pairs else ""
            return [(text, col, col, (label,))]

        mid = len(pairs) // 2
        left = self._split_pairs_recursive(block, " | ".join(pairs[:mid]), budget)
        right = self._split_pairs_recursive(block, " | ".join(pairs[mid:]), budget)
        return [*left, *right]

    def _finish_part(
        self,
        rows: list[str],
        indexes: list[int],
        header: str | None,
        block: Block,
    ) -> tuple[list[str], int | None, int | None]:
        """收尾一个原子段：补表头并算出它的表格行号区间。

        参数：
            rows: 本段数据行（不含表头）。
            indexes: 每行在原始 ``lines`` 里的下标（用于行号映射）。
            header: 表头行（表格才有）；``None`` 表示这段不重复表头。
            block: 原子块（提供 ``row_from`` 基准）。
        返回：``(含表头的行列表, row_from, row_to)``；块没有行号时返回
            ``(行列表, None, None)``。
        说明：表头在块内占第 0 行，所以数据行的下标要减 1；``max(row_from, ...)``
            兜住"空行导致下标偏移"的极端情况，行号宁可贴在表格首行也不越界。
            ``row_from`` 是"这个表格第一行的编号"，各格式基准可能不同（xlsx 用 sheet
            绝对行号、csv/md/docx 用表内 1 基），这里**统一以它为基准加偏移**，
            两种口径都能得到正确行号，不要假设它一定从 1 开始。
        """
        lines = ([header] if header else []) + rows
        if block.row_from is None:
            return lines, None, None
        # ``indexes`` 是块内**含表头**的行下标（表头=0）；``block.row_from`` 是表头所在的
        # 绝对行号，所以数据行的绝对行号 = row_from + index（不是再减 1，否则会报成表头行）。
        first = block.row_from + indexes[0]
        last = block.row_from + indexes[-1]
        return lines, max(block.row_from, first), last

    # ---- 内部：parent 生命周期 ----

    def _ensure_parent(self, block: Block) -> _OpenParent:
        """拿到当前 parent；没有就**新建一个**（挂起的标题在这里转成 parent 开头）。

        参数：
            block: 当前块（没有挂起标题时，用它的 ``section_path``/``level``/类型起 parent）。
        返回：正在累积的 :class:`_OpenParent`（即 ``self._open``）。
        说明：标题只进 parent 正文（``text_parts``），**不产 child**——parent 是送给模型的
            证据，标题提供语境；child 是检索单位，标题单独成块会污染相似度。
        """
        if self._open is not None:
            # 父块已经开着（同一 H2 章节内又来了 H3/H4）：把待定标题并进它的正文，
            # 不新开父块——这正是"父块动态、小节合并"的关键。
            self._flush_pending_into(self._open)
            return self._open
        heads, self._pending = self._pending, []
        self._pending_used = True
        if heads:
            # 父块的章节 = **顶层标题**（H2）：H3/H4 只是并进来的小节，
            # 不能把父块标成最内层小节，否则父块又退化成"子块 + 一行标题"。
            top = heads[0]
            parent_path = tuple(top.section_path) or (top.text.strip(),)
            parent = _OpenParent(
                section_path=parent_path,
                level=top.level,
                content_type=TYPE_TEXT,
            )
            # **父块正文不放标题**：标题只作为元数据（section_path）由提示词单独给，
            # 正文保持纯内容，避免模型把"2. 相关工作"这种标签当成正文的一部分。
            self._parent_path = tuple(parent.section_path)
            # 定位起点取**整条链的最外层**（父块从 H2 那行开始）
            parent.page = top.page
            parent.page_to = top.page_to
            parent.line_from = top.line_from
            parent.line_to = top.line_to
        else:
            # 没有待定标题：优先沿用"当前父章节路径"（预算溢出后重开的场景），
            # 否则用当前块自己的结构信息（无结构文档的常态）。
            section_path = self._parent_path or tuple(block.section_path)
            parent = _OpenParent(
                section_path=section_path,
                level=block.level,
                content_type=block.content_type,
            )
        # 跨 feed() 存活，直到 _close_parent()/flush() 把它封口
        self._open = parent
        return parent

    def _flush_pending_into(self, parent: _OpenParent) -> None:
        """清空待定标题链（H3/H4 不再开新父块）。

        **不把标题写进父块正文**：父块正文只放该节内容，标题由元数据
        （``section_path``）单独提供给提示词。这里只负责消费待定链，
        避免它被下一个块误用。
        """
        del parent  # 只用来强调"调用方是当前父块"，正文不受影响
        self._pending = []
        self._pending_used = True

    def _parent_tail(self, text: str) -> str:
        """取 parent 末尾凑够 ``parent_overlap`` 的**整句**（超长节再分时用）。

        参数：
            text: 当前 parent 的正文。
        返回：末尾若干整句拼成的字符串；关闭重叠、文本为空、或凑不够
            ``parent_overlap``（叠了也近乎整段重复）时返回 ``""``。
        两个兜底（无标点长段落 / 小段落，转写稿实测踩过）：

        - 整段没有句末标点时，``_sentence_spans`` 只给一个整段 span——按 token 上限
          截尾，不让重叠无限涨（上限 = ``max(parent_overlap, min(半个 parent,
          parent_overlap × 2))``），否则相邻 parent 会重复上千字；
        - 整个 parent 都比 ``parent_overlap`` 小：整段做重叠 = 下一个 parent 完整重复，
          不如不叠。
        """
        if self._parent_overlap <= 0 or not text:
            return ""
        # 重叠上限：min(半个 parent, 2×overlap)，但**不低于** parent_overlap——
        # 正常尺寸下就是 2×overlap；防止"无标点整段"把重叠拉成上千字
        cap = max(
            self._parent_overlap,
            min(self._parent_size // 2, self._parent_overlap * 2),
        )
        spans = list(_sentence_spans(text))
        chosen = ""
        # 从末尾往前累加整句，直到触到 cap
        for start, end in reversed(spans):
            candidate = text[start:end]
            if not chosen:
                if self._count(candidate) > cap:
                    # 第一句（往往就是整段）就超 cap：按 token 截尾，保证重叠有上界
                    return _tail_to_tokens(candidate, cap, self._count)
                chosen = candidate
                continue
            combined = candidate + chosen
            if self._count(combined) > cap:
                break
            chosen = combined
        if not chosen or self._count(chosen) < self._parent_overlap:
            return ""  # 太小：重叠=整段重复，不如不叠
        return chosen

    def _close_parent(self) -> None:
        """把当前 parent 封口成组；没有 child 的 parent 直接丢弃。

        返回：无（副作用：向 ``self._groups`` 追加一组，并清空 ``self._open``）。
        说明：只有标题、没有正文的 parent 不产出——否则库里会多出一堆"只有标题"的
            证据块；封口后下一次 ``_ensure_parent`` 会重开一个新的。
        """
        parent, self._open = self._open, None
        # 子块起点是**每个 parent 内**的坐标，封口后必须清掉
        self._last_child_start = None
        if parent is None or not parent.children:
            return
        self._groups.append(ChunkGroup(parent=parent.to_record(), children=tuple(parent.children)))


def _tail_to_tokens(
    text: str, cap: int, count: Callable[[str], int]
) -> str:
    """取 ``text`` 末尾、token 数不超过 ``cap`` 的**最长尾段**。

    参数：
        text: 原文（通常是一个"无标点整段"）。
        cap: 尾段允许的最大 token 数。
        count: 数 token 的函数。
    返回：``text`` 的一个后缀；``text`` 为空时返回空串。整段本身都 ≤ ``cap`` 时
        返回整段（此时"重叠"就没有上界意义了，调用方按需接受）。
    为什么需要：无标点段落里"一句"可能占满整个 parent，整句带入重叠会无限膨胀。
    这里不追求句子边界（本来就没有），只按 token 预算二分截尾——保证重叠有上界。
    """
    low, high = 0, len(text)
    best = len(text)
    while low <= high:
        middle = (low + high) // 2
        if count(text[middle:]) <= cap:
            # 越靠左尾越长：能放进 cap 的最长尾，继续往左试
            best = middle
            high = middle - 1
        else:
            low = middle + 1
    return text[best:]


def _semantic_runner(
    counter: Callable[[str], int] | None,
) -> Callable[[str, int], list[str] | None]:
    """语义兜底的**惰性包装**：函数被调用时才 import 语义模块（连带嵌入模型）。

    参数：
        counter: 数 token 的函数（透传给语义切分器）；``None`` 时用默认估算。
    返回：``(text, budget) -> list[str] | None`` 的回调，可直接挂到 ``Splitter(semantic=...)``。
    为什么要包一层：``chunk.semantic_fallback`` 默认开着，但绝大多数文档根本不会触发
    （有标题树、或标点够断句）。直接在构造时 import 会把"加载模型的代价"压到每次导入上。
    """

    def run(text: str, budget: int) -> list[str] | None:
        """真正执行语义切分（首次调用时才 import 语义模块/加载模型）。"""
        from app.core.chunk import semantic as semantic_module

        return semantic_module.split_semantically(text, budget, count_tokens=counter)

    return run
