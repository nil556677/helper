"""Word（.docx）：**按 body 原始顺序**遍历段落与表格（P1.1 定稿的细节）。

## 在流水线里的位置

- **上游**：:func:`app.core.parse.iter_units` 按 ``.docx`` 后缀派发到这里；
- **本模块**：遍历 XML body，产出"一个 body 元素一个单元"的块（段落/表格/图片占位），
  带上 ``para_index`` 与原生标题层级；**不判标题**（有原生样式就直接用，
  ``Heading N`` 的层级交给结构识别）、**不切分**；
- **下游**：统一清洗 → 结构识别（原生优先，样式缺失才走字号启发式）→ 切分 → 入库。

## 输入输出

- 输入：``.docx`` 路径（python-docx 需整份载入；压缩包里的 XML，不上 OCR）。
- 输出：``Iterator[Unit]``；每块带 ``para_index``（见 :func:`iter_units` 的说明），
  标题块另带 ``level``（1 基），所有文本块带 ``font_size``/``bold``（有显式设置时）。

## 关键约定

- **顺序是原文档顺序**（见下"为什么走 XML body"），表格不会被挪到文末；
- **表格整块**（``TYPE_TABLE``）：下游不切它，只按行预算处理；
- **图片只占位**：``TYPE_IMAGE`` + 汇总一条 note，OCR 是 P5；
- **合并单元格只渲染一次**（见 :func:`_table_rows`）；
- **流式口径**：``iter_units(path)`` 按 **body 元素**产出（一个元素一个单元），
  切完一个就能写库；docx 本身是 zip 内嵌 XML，python-docx 会整棵载入（文本型大文件极少），
  但**输出侧**保持逐元素流式；
- **为什么走 ``document.element.body``**：python-docx 的 ``paragraphs``/``tables`` 是两条
  独立迭代器，混排文档的交错顺序会丢——表格就放不回正文顺序里。这里按 XML 子节点逐个判断；
- **结构（原生）**：``style.name``（"Heading N"/"标题 N"）或 ``outlineLevel``；都没有才轮到
  字号/加粗（P1.2 兜底）。每个块带 ``para_index``（body 顺序下标）——引用"第 N 段"的锚点。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.parse.meta import looks_like_article_meta
from app.core.parse.model import (
    TYPE_HEADING,
    TYPE_IMAGE,
    TYPE_TABLE,
    TYPE_TEXT,
    Block,
    ParseError,
    Unit,
)


def _has_drawing(paragraph) -> bool:
    """段落里是否嵌了图片/图形（``w:drawing``）。

    图文讲义这类文档：每页一张幻灯片图，图是**独立段落**（正文为空）。
    不识别的话图片会被静默丢掉——只剩语音稿文本。

    用 ``.//w:drawing``（后代轴）而不是直接子元素：图片常被包在
    ``w:r/w:drawing`` 甚至更深的 ``mc:AlternateContent`` 里，只看一层会漏。

    参数：
        paragraph: python-docx 的 ``Paragraph``。
    返回：
        含任意 drawing 元素返回 True。
    """
    return bool(paragraph._p.xpath(".//w:drawing"))


def _paragraph_level(paragraph) -> int | None:
    """取段落层级：优先 outlineLevel，其次样式名（Heading N / 标题 N）。

    为什么 outlineLevel 优先：它是文档里真正的**大纲级别**（导航窗格就是按它显示的），
    比样式名可靠——用户可以自定义样式名（"我的章标题"），但 outlineLvl 仍是 0。
    ``outlineLvl`` 在 OOXML 里是 **0 基**（0 = 1 级），所以要 +1；取值范围 0–8，
    超出（含负值/异常值）就当没设置，退回样式名。

    样式名匹配同时认英文和中文（"Heading 1" / "标题 1"），大小写不敏感：
    Word 中文版会把内置样式名本地化，只认英文会漏掉一半文档。

    参数：
        paragraph: python-docx 的 ``Paragraph``。
    返回：
        标题层级（1 基）；不是标题样式的普通段落返回 None。
    """
    p_pr = paragraph._p.pPr
    if p_pr is not None and p_pr.outlineLvl is not None:
        level = p_pr.outlineLvl.val
        if level is not None and 0 <= int(level) <= 8:
            return int(level) + 1
    style_name = str(paragraph.style.name) if paragraph.style else ""

    match = re.match(r"^(?:heading|标题)\s*([1-9])", style_name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _run_style(paragraph) -> tuple[float | None, bool]:
    """段落字号/加粗（P1.2 启发式兜底的原料；显式设置才有，样式继承的为 None）。

    只读 ``run.font.size``（**显式**设置的字号）：从样式继承来的字号 python-docx 拿不到，
    硬用 ``style.font.size`` 去凑会把"继承值"当成"显式设置"（于是每个块都有字号，
    基准统计被样式默认值支配）。所以拿不到就是 None，让结构识别自己决定要不要用字号。

    字号取**最后一个**显式设置了字号的 run（循环覆盖），不是首 run 也不是众数——
    实际文档里一个段落的字号基本一致，取哪个都一样；不一致时（句尾小字标注）
    覆盖到最后一个是已知的粗糙处，宁可粗糙也别在这里引入统计逻辑。

    参数：
        paragraph: python-docx 的 ``Paragraph``。
    返回：
        ``(字号磅数或 None, 是否加粗)``；加粗是"任一 run 加粗即为 True"。
    """
    size: float | None = None
    bold = False
    for run in paragraph.runs:
        if run.font.size is not None:
            size = float(run.font.size.pt)
        if run.bold:
            bold = True
    return size, bold


def _table_rows(table) -> tuple[list[list[str]], tuple[tuple[int, int, int, int], ...]]:
    """表格 → 行文本 + 合并范围。

    合并策略：
    - 纵向合并：后续行用首行值填充（≤20 行语义；DOCX 不区分超大）；
    - 横向合并在表头区（前 2 行）：铺开，保证多级列名；
    - 横向合并在数据区：首列保留值，其余列留空，避免复制成假值。
    """
    seen: dict[object, tuple[int, int, str]] = {}
    rows: list[list[str]] = []
    merged: list[tuple[int, int, int, int]] = []
    for row_index, row in enumerate(table.rows):
        cells: list[str] = []
        for col_index, cell in enumerate(row.cells):
            tc = cell._tc
            text = cell.text.strip()
            first = seen.get(tc)
            if first is None:
                seen[tc] = (row_index, col_index, text)
                cells.append(text)
                continue
            first_row, first_col, first_text = first
            if first_row < row_index:
                # 纵向合并：向下填值，保证每一行都能独立读懂
                cells.append(first_text)
                merged.append((first_row + 1, row_index + 1, first_col + 1, col_index + 1))
            elif row_index <= 1:
                # 表头横向合并：铺开给多级表头
                cells.append(first_text)
                merged.append((first_row + 1, row_index + 1, first_col + 1, col_index + 1))
            else:
                # 数据区横向合并：值只保留一次，其余列留空
                cells.append("")
        if any(cells):
            rows.append(cells)
    return rows, tuple(merged)


def _paragraph_is_caption(element) -> bool:
    """题注段落：Word 的 Caption/题注样式，或以“表/Table”开头的短句。"""
    text = str(getattr(element, "text", "") or "").strip()
    if not text or len(text) > 120:
        return False
    style = ""
    try:
        style = str(element.style.name) if element.style else ""
    except Exception:
        style = ""
    if style.lower().startswith(("caption", "题注")):
        return True
    return text.startswith(("表 ", "表1", "表2", "表3", "Table "))


def _walk_body(document) -> Iterator[object]:
    """按 body 顺序产出 ``Paragraph`` / ``Table`` 包装对象。

    只遍历 body 的**直接子节点**：嵌套在表格单元格里的段落/表格不会被单独产出
    （它们的内容已经通过外层表格的 ``cell.text`` 进了表格块，重复产出反而会造成
    同一段文字被索引两次）。

    其他子节点（``w:sectPr`` 节属性、``w:bookmarkStart`` 书签锚点等）直接跳过。

    参数：
        document: python-docx 的 ``Document``。
    返回：
        ``Paragraph`` / ``Table`` 对象的流，顺序与 XML 子节点一致。
    """
    for child in document.element.body:
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def iter_units(path: Path) -> Iterator[Unit]:
    """按 **body 元素**逐段产出（段→单元、表→单元，顺序即原文档顺序）。

    处理顺序与分支：

    1. **表格**：整块渲染成一个 ``TYPE_TABLE`` 块，``table_ref=tbl#N``（N 从 1 起，
       按文档里出现的顺序数）、行区间 1..行数；**同样带 ``para_index``**，
       这样"跳到表格"和"跳到段落"用同一套坐标；
    2. **图片段落**：产出 ``TYPE_IMAGE`` 占位块（``text=""``）。图片段落若**还有正文**
       （图+说明文字），占位块之后继续走下面的文本分支，文字不会丢；
    3. **标题段落**：有原生层级 → ``TYPE_HEADING`` + ``level``；
    4. **普通段落**：``TYPE_TEXT``。

    最后若出现过图片，末尾追加一个**只有 notes 的单元**汇总提示（"文档含 N 张图片，
    暂未做 OCR"）——用户得知道"图里的字没进库"，而不是以为搜不到是别的原因。

    ``para_index`` 的口径：``enumerate(_walk_body(document))`` 的下标，**0 基**，
    只数 body 的段落与表格（不含 sectPr 等），所以它是"第几个可读 body 元素"，
    不是"第几个段落"。引用展示时 +1 变成"第 N 段"。

    参数：
        path: ``.docx`` 文件路径。
    返回：
        单元流；标点/空白不会在这里被清理（由统一清洗那一步做）。
    异常：
        ParseError: 缺 python-docx（未安装）或文档打不开（损坏/加密）。
    """
    try:
        import docx  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ParseError("解析 Word 需要 python-docx（还没装）") from exc

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        # 包成 ParseError：底层异常（zip 损坏、XML 解析失败）对用户没有意义
        raise ParseError(f"打不开这份 Word 文档（可能损坏）：{exc}") from exc

    table_no = 0
    image_count = 0
    pending_caption = ""
    for element_index, element in enumerate(_walk_body(document)):
        if isinstance(element, Table):
            table_no += 1
            rows, merged = _table_rows(element)
            rendered = "\n".join(" | ".join(row) for row in rows if any(row))
            header = tuple(rows[0]) if rows else ()
            yield Unit(
                blocks=[
                    Block(
                        text=rendered,
                        content_type=TYPE_TABLE,
                        table_ref=f"tbl#{table_no}",
                        table_title=pending_caption,
                        merged_ranges=merged,
                        row_from=1,
                        row_to=max(1, len(rows)),
                        col_from=1,
                        col_to=max(1, len(rows[0])) if rows else None,
                        header=header,
                        para_index=element_index,
                    )
                ]
            )
            pending_caption = ""
            continue

        # 图片段落（图文讲义：每页一张幻灯片图，正文常为空）：
        # 产出 **TYPE_IMAGE 占位块**（OCR/多模态以后再填文字），别静默丢掉。
        if _has_drawing(element):
            image_count += 1
            yield Unit(
                blocks=[
                    Block(
                        text="",
                        content_type=TYPE_IMAGE,
                        para_index=element_index,
                    )
                ]
            )
            if not element.text.strip():
                continue  # 纯图段落：只给占位块
            # 图 + 说明文字：占位块已产出，继续往下把文字也做成块（不能 return）

        text = element.text.strip()
        if not text:
            continue
        pending_caption = text if _paragraph_is_caption(element) else ""
        level = _paragraph_level(element)
        size, bold = _run_style(element)
        if level is None and looks_like_article_meta(text):
            # 日期/来源等文章元信息不是正文：跳过，别让它做第一个片段的开头
            continue
        if level is not None:
            yield Unit(
                blocks=[
                    Block(
                        text=text,
                        content_type=TYPE_HEADING,
                        level=level,
                        font_size=size,
                        bold=bold,
                        para_index=element_index,
                    )
                ]
            )
        else:
            yield Unit(
                blocks=[
                    Block(
                        text=text,
                        content_type=TYPE_TEXT,
                        font_size=size,
                        bold=bold,
                        para_index=element_index,
                    )
                ]
            )

    if image_count:
        # 汇总一条提示（别让"图文"里图全丢还没人知道）：目前只占位，未做 OCR。
        # 这个单元没有块，只有 notes——上层会把它并进文档提示列表。
        yield Unit(notes=[f"文档含 {image_count} 张图片，暂未做 OCR（只索引了文字）"])
