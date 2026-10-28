"""记录：切分结果写库的输入（``ChunkRecord`` 与 ``ChunkGroup``）。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。
切分层只产出本模块的 dataclass；入库层负责把它们落进 ``chunks`` 表
（parent 与 child 同表、用 ``is_parent`` 区分，子记录通过 parent 的 id 关联）。

## 输入 / 输出

- 输入：切分器算好的文本 + 定位字段（页/行/bbox/表格行号）。
- 输出：``ChunkGroup``（一个 parent + 若干 child）；
  ``ChunkRecord.loc_json()`` 给出与 ``Block`` 同口径的 JSON 定位。

## 关键约定

1. **不可变**：记录是值对象，合并/替换都产生新实例（``dataclasses.replace``）；
2. **定位口径与 ``Block`` 一致**：``loc_json`` 复用同一个 ``loc_payload``，
   引用跳转才不会"切分前准、切分后偏"；
3. ``section_path`` 是**完整标题链**（tuple），``section`` 只取最内层给人看；
4. ``hard_split`` 如实标记"这段是硬切的"，引用时要能解释清楚。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.parse.model import TYPE_TEXT, loc_payload


@dataclass(frozen=True)
class ChunkRecord:
    """一条待写库的片段（parent 或 child）。

    字段与 ``chunks`` 表一一对应：``text`` 是**原文切片**（不许改写），
    ``is_parent`` 区分证据块与检索单位，其余字段承载章节结构与定位。
    """

    text: str
    is_parent: bool = False
    content_type: str = TYPE_TEXT
    section_path: tuple[str, ...] = ()
    level: int | None = None
    page: int | None = None
    page_to: int | None = None
    line_from: int | None = None
    line_to: int | None = None
    table_ref: str | None = None
    row_from: int | None = None
    row_to: int | None = None
    col_from: int | None = None
    col_to: int | None = None
    header: tuple[str, ...] = ()
    coords: tuple[float, float, float, float] | None = None
    para_index: int | None = None
    """docx 的"第几个 body 元素"（0 基）：没有页码时靠它定位回原文。"""
    hard_split: bool = False
    """这块是**硬切**出来的（原文这里没有标点可用于断句）——引用时如实说明。"""
    role: str = "text"
    """text / section_parent / table_parent / table_row / table_row_segment。"""
    fetch_mode: int = 1
    """0=取自己；1=取父块；2=取父块+兄弟块（按 segment_index 排序）。"""
    table_uid: str | None = None
    row_uid: str | None = None
    row_no: int | None = None
    segment_index: int | None = None
    segment_count: int = 1
    merged_ranges: tuple[tuple[int, int, int, int], ...] = ()

    @property
    def section(self) -> str:
        """最内层标题（人看的那一列）；没有结构时是空串。"""
        return self.section_path[-1] if self.section_path else ""

    def loc_json(self) -> dict[str, object]:
        """写进 ``chunks.loc_json`` 的定位（与 :meth:`Block.loc_json` 同一口径）。

        返回：``loc_payload`` 生成的字典。
        约定：``kind`` 按"最具体可用"选择——表格（``table_ref``）> 页码 > 行号；
            都没有时交给 ``loc_payload`` 生成空定位，不抛异常。
        """
        kind = None
        if self.table_ref is not None:
            kind = "table"
        elif self.page is not None:
            kind = "page"
        elif self.line_from is not None:
            kind = "lines"
        elif self.para_index is not None:
            kind = "para"
        return loc_payload(
            kind=kind,
            page=self.page,
            page_to=self.page_to,
            line_from=self.line_from,
            line_to=self.line_to,
            table_ref=self.table_ref,
            row_from=self.row_from,
            row_to=self.row_to,
            col_from=self.col_from,
            col_to=self.col_to,
            header=self.header,
            para_index=self.para_index,
            coords=self.coords,
        )


@dataclass(frozen=True)
class ChunkGroup:
    """一个 parent 与它的 children（写库时"先父后子"，父 id 直接能拿到）。

    用 ``tuple`` 而不是 ``list``：成组结果一旦产出就是只读的，
    避免下游（入库/诊断）不小心改到别人的 children。
    """

    parent: ChunkRecord
    children: tuple[ChunkRecord, ...]
