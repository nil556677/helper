"""切分过程中的**中间数据结构**：块内片段候选与正在累积的 parent。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。
``core.Splitter`` 用 :class:`_Piece` 表示"块内字符区间"（避免反复复制文本），
用 :class:`_OpenParent` 累积一个尚未封口的 parent；两者都**不写库**，
封口后才转换成 ``records.ChunkRecord``。

## 输入 / 输出

- ``_Piece``：由 ``core.Splitter._pieces`` 产出，输入是块内 ``[start, end)``。
- ``_OpenParent``：由 ``_ensure_parent`` 创建，输出是 ``to_record()`` 的 parent 记录。

## 关键约定

1. ``_Piece`` **只存区间、不存文本**：文本始终从 ``block.text`` 切出，
   保证与原文逐字一致（引用不能改一个字）；
2. ``text_parts`` 里常规的 child 正文**不含 child 重叠**（重叠只用于检索冗余，不算证据）；
   parent 自身体积超限再分时，按 ``parent_overlap`` 追加的整句重叠是唯一的例外；
3. ``tokens`` 是**累加值**，只用于粗判；真正守预算时按拼好的文本重新计数
   （token 计数不可加，见 ``core.Splitter._emit_child``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.parse.model import TYPE_TEXT

from .records import ChunkRecord


@dataclass
class _Piece:
    """一个片段：块内的字符区间（**文本由区间切出来，保证与原文逐字一致**）。

    字段：
        start / end: 左闭右开区间；
        hard: 该片段是否由"没有标点可用"的硬切产生（``_hard_fit``），
            最终会写进 ``ChunkRecord.hard_split``，引用时如实说明。
    """

    start: int
    end: int
    hard: bool = False


@dataclass
class _OpenParent:
    """正在累积的 parent（还没封口）。

    一个 parent 由**标题开启**，可以跨多个结构单元（PDF 一页、docx 一个元素……），
    所以它必须跨 ``feed()`` 存活。字段与 ``ChunkRecord`` 对齐：
    ``text_parts`` 是正文分片（封口时用换行拼成 parent 文本），
    ``tokens`` 是累加估值（只用于粗判，守预算时按拼好的文本重算）。
    """

    section_path: tuple[str, ...] = ()
    level: int | None = None
    content_type: str = TYPE_TEXT
    text_parts: list[str] = field(default_factory=list)
    children: list[ChunkRecord] = field(default_factory=list)
    tokens: int = 0
    page: int | None = None
    page_to: int | None = None
    line_from: int | None = None
    line_to: int | None = None
    coords: tuple[float, float, float, float] | None = None
    table_ref: str | None = None
    row_from: int | None = None
    row_to: int | None = None

    def add(self, record: ChunkRecord, source_text: str, *, tokens: int) -> None:
        """挂一条 child，并推进 parent 的定位/正文。

        参数：
            record: 已造好的 child 记录（加入 ``children``）。
            source_text: 这段**非重叠**原文（加入 ``text_parts``，拼成 parent 正文）。
            tokens: 这段文本的 token 数（累加进 ``tokens``，只作粗判）。
        返回：无。
        说明：定位字段（page/line/coords）用"第一条定起点、最后一条推进终点"的口径，
            跨页/跨行时 parent 的范围自然覆盖整段。
        """
        self.children.append(record)
        self.text_parts.append(source_text)
        self.tokens += tokens
        # 起点只认**第一个**进来的东西（可能是标题、也可能是首个正文块），之后不再改：
        # 先前用 "page is None" 当守卫，而 txt/md 的块**没有页码**，于是每加一个子块
        # 就把父块起点往后挪，最后父块的 line_from 变成**最后一个子块**的行（实测）。
        if self.line_from is None and record.line_from is not None:
            self.line_from = record.line_from
            self.page = record.page
            self.coords = record.coords
        # 终点每加一条就往后推；没有 page_to 时至少落在本块所在页
        self.page_to = record.page_to if record.page_to is not None else record.page
        if record.line_to is not None:
            self.line_to = record.line_to
        # 只有"整段 parent 都在同一页"时 bbox 才有意义；跨页的框会横跨两页，保留原框即可
        if record.coords is not None and self.coords is not None and self.page == self.page_to:
            self.coords = (
                min(self.coords[0], record.coords[0]),
                min(self.coords[1], record.coords[1]),
                max(self.coords[2], record.coords[2]),
                max(self.coords[3], record.coords[3]),
            )

    @property
    def text(self) -> str:
        """parent 的正文：各段按换行拼接，**跳过空段**（空标题不该留空行）。"""
        return "\n".join(part for part in self.text_parts if part)

    def to_record(self) -> ChunkRecord:
        """封口：转成不可变的 parent :class:`ChunkRecord`（``is_parent=True``）。"""
        return ChunkRecord(
            text=self.text,
            is_parent=True,
            content_type=self.content_type,
            role="section_parent",
            fetch_mode=0,
            section_path=self.section_path,
            level=self.level,
            page=self.page,
            page_to=self.page_to,
            line_from=self.line_from,
            line_to=self.line_to,
            table_ref=self.table_ref,
            row_from=self.row_from,
            row_to=self.row_to,
            coords=self.coords,
        )
