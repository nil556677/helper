"""解析的**数据形状**：块（Block）+ 文档（ParsedDocument）。

**为什么是"有序块列表"而不是一段文本**（P1.1 的核心）：引用要回答"哪个文件、哪一页、
哪一行（xlsx 多表时哪张表）"，还要能跳回原文位置——这些必须由**解析阶段**就记录下来，
一路带到 chunk，而不是事后从切出来的片段反推。

每个块都带定位信息（``loc`` 那一组字段）：

- ``page`` + ``line_from/to``：PDF 页内行号、txt/md 的文件行号；
- ``table_ref``：表格身份（``sheet:名称#序号`` / ``tbl#body序号`` / ``page:1#2``）；
- ``row_from/to``：表格行区间；
- ``para_index``：docx 正文段落的 body 顺序；
- ``coords``：PDF 页内 bbox（渲染高亮用）。

结构字段（``level`` / ``font_size`` / ``bold`` / ``section_path``）由 P1.2 填，
P1.1 先带着原料。``content_type`` 在 P1.1 就定：``text / table / heading / image``。
图片页是"待 OCR"占位（OCR 本体是 P5，接缝在这里留好）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ParseError(RuntimeError):
    """解析失败（原因是要给用户看的一句话，界面/任务 note 直接用）。"""


#: 块类型：正文 / 表格（整块保留）/ 标题（P1.2 标）/ 图片（待 OCR 占位）
TYPE_TEXT = "text"
TYPE_TABLE = "table"
TYPE_HEADING = "heading"
TYPE_IMAGE = "image"
TYPE_CODE = "code"

#: 定位方式：文件行 / 页内行 / 段落 / 表格（存进 loc_json 用）
LOC_LINES = "lines"
LOC_PAGE = "page"
LOC_PARA = "para"
LOC_TABLE = "table"


@dataclass(frozen=True)
class Block:
    """文档里的一个有序块。"""

    text: str
    content_type: str = TYPE_TEXT

    # ---- 定位（引用跳转的原料）----
    page: int | None = None
    """页码（PDF/PPT 有；txt/md 无）。"""

    line_from: int | None = None
    """文件或页内的行号起（1 基）。"""

    line_to: int | None = None
    """行号止（含）。"""

    table_ref: str | None = None
    """表格身份：``sheet:名称#序号`` / ``tbl#序号`` / ``page:页号#序号``。"""

    row_from: int | None = None
    """表格行区间起（1 基）。"""

    row_to: int | None = None
    """表格行区间止。"""

    para_index: int | None = None
    """docx 正文段落的 body 顺序下标（0 基）。"""

    coords: tuple[float, float, float, float] | None = None
    """PDF 页内 bbox ``(x0, y0, x1, y1)``（高亮用）。"""

    # ---- 结构（P1.2 填；P1.1 只带原料）----
    level: int | None = None
    """标题层级 1–4。"""

    font_size: float | None = None
    bold: bool = False
    section_path: list[str] = field(default_factory=list)
    """标题链（P1.2 填），如 ``["第二章", "2.3 违约"]``。"""

    @property
    def loc_kind(self) -> str | None:
        """这份块属于哪种定位（存进 loc_json）。"""
        if self.table_ref is not None:
            return LOC_TABLE
        if self.para_index is not None:
            return LOC_PARA
        if self.page is not None:
            return LOC_PAGE
        if self.line_from is not None:
            return LOC_LINES
        return None

    def loc_json(self) -> dict[str, object]:
        """序列化定位信息（chunks.loc_json 列的内容；**不参与检索过滤**）。"""
        payload: dict[str, object] = {}
        kind = self.loc_kind
        if kind:
            payload["kind"] = kind
        for name, value in (
            ("page", self.page),
            ("line_from", self.line_from),
            ("line_to", self.line_to),
            ("table", self.table_ref),
            ("row_from", self.row_from),
            ("row_to", self.row_to),
            ("para", self.para_index),
            ("coords", self.coords),
        ):
            if value is not None:
                payload[name] = value
        return payload


@dataclass
class ParsedDocument:
    """一份文件解析出的全部内容。"""

    blocks: list[Block]
    """**有序**块列表（阅读顺序）。"""

    pages: int | None = None
    notes: list[str] = field(default_factory=list)
    ocr_pages: list[int] = field(default_factory=list)
    """待 OCR 的页号（PDF 无文字层但有图片）。"""

    table_count: int = 0

    @property
    def chars(self) -> int:
        """正文字符数（解析摘要用）。"""
        return sum(len(block.text) for block in self.blocks)

    @property
    def text(self) -> str:
        """把块拼回一段文本（P1.3 之前的临时消费；之后切分直接吃块）。"""
        return "\n\n".join(block.text for block in self.blocks if block.content_type != TYPE_IMAGE)

    def locate(self, block: Block) -> str:
        """给人看的一句话定位（引用行展示用），如 ``第 3 页 · 第 12–20 行``。"""
        parts: list[str] = []
        if block.page is not None:
            parts.append(f"第 {block.page} 页")
        if block.table_ref is not None:
            parts.append(block.table_ref)
        elif block.line_from is not None and block.line_to is not None:
            if block.line_from == block.line_to:
                parts.append(f"第 {block.line_from} 行")
            else:
                parts.append(f"第 {block.line_from}–{block.line_to} 行")
        elif block.para_index is not None:
            parts.append(f"第 {block.para_index + 1} 段")
        return " · ".join(parts) if parts else "（无定位）"
