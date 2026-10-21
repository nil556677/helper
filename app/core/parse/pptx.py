"""PowerPoint（.pptx）：每张幻灯片的文本框与表格 → 带页码的块。

## 在流水线里的位置

- **上游**：:func:`app.core.parse.iter_units` 按 ``.pptx`` 后缀派发到这里；
- **本模块**：逐张幻灯片遍历 shape，把表格渲染成表格块、文本框内容渲染成正文块；
  **不判标题**：PPT 没有原生层级，本解析器也不填字号/加粗，而结构识别那边要求
  "编号 + 样式佐证"或大字号才判标题——所以 **PPT 目前产不出标题**（每页各自独立，
  ``section_path`` 为空）；**不切分**；
- **下游**：统一清洗 → 结构识别 → 切分 → 入库。

## 输入输出

- 输入：``.pptx`` 路径（python-pptx 整份载入；PPT 通常不大）。
- 输出：``Iterator[Unit]``；每张幻灯片一个单元，块带 ``page``（= 幻灯片序号，1 基）
  与 ``pages``（总页数）；表格块另带 ``table_ref=slide:N#序号`` 与行区间。

## 关键约定

- **页码 = 幻灯片序号**（引用"第 N 页"对 PPT 就是第 N 张幻灯片）；
- **一个 shape 一个块**：同一张幻灯片里的多个文本框**不合并**（PPT 里它们是独立的
  视觉块，合并会把标题和正文粘在一起，结构识别就再也分不开）；
- **表格整块**：``slide:N#序号``，整块保留；
- **只读 shape 的可见文本**：组合形状里的嵌套文本（python-pptx 用 ``shape.shapes`` 逐层
  走）、以及**演讲者备注**（标准文本框）都会读；SmartArt/图表取不到文本（API 限制）。
- **流式口径**：``iter_units(path)`` 按幻灯片产出（一次一张在内存）。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from app.core.parse.model import TYPE_HEADING, TYPE_TABLE, TYPE_TEXT, Block, ParseError, Unit


def _text_style(text_frame: object) -> tuple[float | None, bool]:
    """取文本框里**显式设置**的字号（第一个有值的 run）与是否加粗。

    参数：
        text_frame: python-pptx 的 ``TextFrame``。
    返回：
        ``(字号磅值或 None, 是否加粗)``；字号多为继承值（None），交给占位符判定。
    """
    size: float | None = None
    bold = False
    for paragraph in getattr(text_frame, "paragraphs", ()):
        for run in paragraph.runs:
            if size is None and run.font.size is not None:
                size = float(run.font.size.pt)
            if run.font.bold:
                bold = True
    return size, bold


def _is_title_placeholder(shape: object) -> bool:
    """这个 shape 是不是"标题占位符"（幻灯片标题/居中标题/副标题）。

    为什么用它而不是字号：PPT 的标题字号常常是**继承的**（``font.size`` 为 None），
    光看字号判不出来；占位符类型是稳定信号。
    """
    try:
        from pptx.enum.shapes import PP_PLACEHOLDER

        if not getattr(shape, "is_placeholder", False):
            return False
        kind = shape.placeholder_format.type
        return kind in (
            PP_PLACEHOLDER.TITLE,
            PP_PLACEHOLDER.CENTER_TITLE,
            PP_PLACEHOLDER.SUBTITLE,
        )
    except Exception:
        return False


def _walk_shapes(container) -> Iterator[object]:
    """递归展开形状：组合形状（``GroupShape``）里的嵌套形状也收进来。

    python-pptx 的 ``GroupShape`` 既没有 ``has_table`` 也没有 ``has_text_frame``，
    但它有 ``shapes`` 子集合——逐层展开，组合里的标题/表格/正文就不会漏。
    """
    for shape in getattr(container, "shapes", ()):
        yield shape
        if getattr(shape, "shape_type", None) is not None and hasattr(shape, "shapes"):
            yield from _walk_shapes(shape)


def iter_units(path: Path) -> Iterator[Unit]:
    """按幻灯片逐张产出。

    每张幻灯片的处理顺序：

    1. 遍历 ``slide.shapes``（**递归展开**：组合形状里的内容也读；按 z 序/添加顺序，
       PPT 没有可靠的阅读顺序，同页多块按 shape 顺序排已经够用）；
    2. ``has_table`` 的 shape：渲染成 ``TYPE_TABLE`` 块，``table_ref=slide:N#M``
       （M 是**本页内**第几张表，1 基）；
    3. ``has_text_frame`` 的 shape：``text_frame.text`` 非空才产块——空占位符
       （"单击此处添加标题"在 XML 里是空文本）不该进索引；
    4. **演讲者备注**：把 ``notes_slide`` 的文本读成一个带前缀的正文块（页内内容之外
       的补充说明，检索时有用）；没有备注就跳过；
    5. 本页有任何块才 ``yield``（空幻灯片直接跳过，不产出空单元）。

    **标题占位符直接标成 ``TYPE_HEADING`` + ``level=1``**（PPT 只有这一层结构），
    其余文本框填**显式**的 ``font_size``/``bold``（继承值仍为 None，不当基准）。

    **仍然拿不到的**（API 限制，非刻意）：SmartArt、图表、OLE 对象——都没有文本接口。

    参数：
        path: ``.pptx`` 文件路径。
    返回：
        每张幻灯片一个单元（``pages`` = 总页数）；空幻灯片不产出单元。
    异常：
        ParseError: 缺 python-pptx（未安装）或文件打不开（损坏/加密）。
    """
    try:
        from pptx import Presentation  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ParseError("解析 PPT 需要 python-pptx（还没装）") from exc

    try:
        presentation = Presentation(str(path))
    except Exception as exc:
        # 包成 ParseError：底层异常（zip 损坏、XML 结构异常）对用户没有意义
        raise ParseError(f"打不开这份 PPT（可能损坏）：{exc}") from exc

    slide_count = len(presentation.slides)
    for slide_index, slide in enumerate(presentation.slides, start=1):
        blocks: list[Block] = []
        table_no = 0  # 本页内的表格序号（不是全局序号，引用里跟 slide 号一起用）
        for shape in _walk_shapes(slide):
            if getattr(shape, "has_table", False):
                table_no += 1
                rows = [[cell.text.strip() for cell in row.cells] for row in shape.table.rows]
                rendered = "\n".join(
                    " | ".join(cell for cell in row if cell) for row in rows if any(row)
                )
                header = tuple(cell for cell in rows[0] if cell) if rows else ()
                blocks.append(
                    Block(
                        text=rendered,
                        content_type=TYPE_TABLE,
                        page=slide_index,
                        page_to=slide_index,
                        table_ref=f"slide:{slide_index}#{table_no}",
                        row_from=1,
                        row_to=max(1, len(rows)),
                        col_from=1,
                        col_to=max(1, len(rows[0])) if rows else None,
                        header=header,
                    )
                )
            elif getattr(shape, "has_text_frame", False):
                text = str(shape.text_frame.text or "").strip()
                if text:
                    size, bold = _text_style(shape.text_frame)
                    block = Block(
                        text=text,
                        content_type=TYPE_TEXT,
                        page=slide_index,
                        page_to=slide_index,
                        font_size=size,
                        bold=bold,
                    )
                    if _is_title_placeholder(shape):
                        # 标题占位符直接标 level=1：PPT 只有"幻灯片标题"这一层结构
                        block = replace(block, content_type=TYPE_HEADING, level=1)
                    blocks.append(block)
        # 演讲者备注：页面内容之外的补充，读成一个带前导标识的正文块（方便区分）
        notes = _slide_notes(slide)
        if notes:
            blocks.append(
                Block(
                    text=notes,
                    content_type=TYPE_TEXT,
                    page=slide_index,
                    page_to=slide_index,
                )
            )
        if blocks:
            yield Unit(blocks=blocks, pages=slide_count)


def _slide_notes(slide) -> str:
    """取这张幻灯片的演讲者备注文本（没有就返回空串）。"""
    try:
        notes_slide = slide.notes_slide
        text = str(notes_slide.notes_text_frame.text or "").strip()
        return "（演讲者备注）" + text if text else ""
    except Exception:
        return ""
