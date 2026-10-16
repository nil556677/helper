"""PDF：**fitz 探针/抽文字 + pdfplumber 抠表格**的组合（P1.1 定稿的搭配）。

分工（用户拍板：组合才是王道，精准优先、慢可接受）：

- **fitz（PyMuPDF）**：打开、逐页探针（有没有文字层 / 有没有图片 / 有没有表格）、
  文字行抽取（带 bbox，供"页内行号 + 高亮"用）、表格**区域**检测（``find_tables()`` 快）、
  以及以后渲染原页（引用跳转的图）——它 C 内核，快；
- **pdfplumber**：在 fitz 给出的表格区域内**精确抠单元格**（``crop().extract_table()``），
  它的表格重建（线条+字符坐标、合并单元格）比 fitz 稳。慢，但表格是引用要精确定位的对象。

**路由（每页三选一）**：

| 页的情况 | 产出 |
| --- | --- |
| 有文字层 | 文字块（带页内行号 + bbox）+ 叠在其中的表格块 |
| 无文字 + 有表格线 | 表格块（整块保留） |
| 无文字 + 有图片 | ``content_type='image'`` 占位块 + notes/ocr_pages 记页号（**OCR 本体是 P5，接缝在这**） |
| 无文字无图片 | 空白页（notes 记"空白"） |

**页内行号**：按 ``get_text("dict")`` 的行顺序编号（1 基）；文字块 = 纵向相邻的行按段合并，
块记录 ``line_from/to`` 与合并后的 bbox（引用高亮用）。表格块记录 ``table_ref='page:N#序号'``
与 ``row_from/to``。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.parse.model import (
    TYPE_IMAGE,
    TYPE_TABLE,
    TYPE_TEXT,
    Block,
    ParsedDocument,
    ParseError,
)

#: 同一段落里相邻行的最大纵向间隔（乘以前一行高度）。
_PARAGRAPH_GAP_FACTOR = 1.6
#: 兜底的段落间隔（行高算不出来时用，pt）。
_PARAGRAPH_GAP_MIN = 8.0


def _lines_with_bbox(page) -> list[tuple[str, tuple[float, float, float, float]]]:
    """抽一页的文字行：``(text, bbox)``，按阅读顺序。"""
    lines: list[tuple[str, tuple[float, float, float, float]]] = []
    raw = page.get_text("dict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            bbox = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
            lines.append((text, bbox))
    # get_text("dict") 通常按 y 排；显式按 y0 排一次保证阅读顺序
    lines.sort(key=lambda item: (item[1][1], item[1][0]))
    return lines


def _paragraph_blocks(
    lines: list[tuple[str, tuple[float, float, float, float]]], page_number: int
) -> list[Block]:
    """把一页的文字行按"纵向间隔"并成段落块；行号 = 页内行序（1 基）。"""
    blocks: list[Block] = []
    current: list[tuple[str, tuple[float, float, float, float]]] = []
    start_line: int | None = None
    for line_no, (text, bbox) in enumerate(lines, start=1):
        if not current:
            current.append((text, bbox))
            start_line = line_no
            continue
        _prev_text, prev_bbox = current[-1]
        gap = bbox[1] - prev_bbox[3]
        threshold = max(_PARAGRAPH_GAP_MIN, (bbox[3] - bbox[1]) * _PARAGRAPH_GAP_FACTOR)
        if gap <= threshold:
            current.append((text, bbox))
        else:
            blocks.append(_close_paragraph(current, page_number, start_line, line_no - 1))
            current, start_line = [(text, bbox)], line_no
    if current:
        blocks.append(_close_paragraph(current, page_number, start_line, len(lines)))
    return blocks


def _close_paragraph(
    lines: list[tuple[str, tuple[float, float, float, float]]],
    page_number: int,
    line_from: int,
    line_to: int,
) -> Block:
    text = "\n".join(part[0] for part in lines)
    xs = [bbox[0] for _t, bbox in lines] + [bbox[2] for _t, bbox in lines]
    ys = [bbox[1] for _t, bbox in lines] + [bbox[3] for _t, bbox in lines]
    coords = (min(xs), min(ys), max(xs), max(ys))
    return Block(
        text=text,
        content_type=TYPE_TEXT,
        page=page_number,
        line_from=line_from,
        line_to=line_to,
        coords=coords,
    )


def _extract_table(
    page, bbox: tuple[float, float, float, float], pdfplumber_page, page_number: int, table_no: int
) -> Block:
    """在 fitz 给的表格区域内，用 pdfplumber 精确抠单元格。"""
    rows: list[list[str]] = []
    try:
        cropped = pdfplumber_page.crop((bbox[0], bbox[1], bbox[2], bbox[3]))
        extracted = cropped.extract_table()
        if extracted:
            rows = [[(cell or "").strip() for cell in row] for row in extracted if any(row)]
    except Exception as exc:
        logger.warning("pdfplumber 提取表格失败（page {} #{}）：{}，退回 fitz", page_number, table_no, exc)
        try:
            rows = [row for table in page.find_tables().tables for row in table.extract()]
        except Exception:  # 都失败：至少保留区域（空表）
            rows = []

    rendered = "\n".join(" | ".join(row) for row in rows if any(cell for cell in row))
    return Block(
        text=rendered,
        content_type=TYPE_TABLE,
        page=page_number,
        table_ref=f"page:{page_number}#{table_no}",
        row_from=1,
        row_to=max(1, len(rows)),
        coords=bbox,
    )


def parse(data: bytes, _name: str) -> ParsedDocument:
    """PDF 入口（探测 + 路由）。"""
    try:
        import fitz
    except ImportError as exc:
        raise ParseError("解析 PDF 需要 PyMuPDF（还没装）") from exc

    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ParseError(f"打不开这份 PDF（可能损坏）：{exc}") from exc

    if document.needs_pass:
        try:
            document.authenticate("")  # 很多 PDF 只是"空口令"加密
        except Exception as exc:
            document.close()
            raise ParseError(f"这份 PDF 有口令，打不开：{exc}") from exc

    notes: list[str] = []
    ocr_pages: list[int] = []
    blocks: list[Block] = []
    page_count = document.page_count

    # pdfplumber 需要文件路径（它基于 pdfminer）；写到数据目录的临时文件
    pdfplumber_path = _stage_pdf(data)
    pdfplumber = None
    try:
        if pdfplumber_path is not None:
            try:
                import pdfplumber

                pdfplumber = pdfplumber.open(str(pdfplumber_path))
            except Exception as exc:
                logger.warning("pdfplumber 打不开这份 PDF（退回纯 fitz）：{}", exc)
                pdfplumber = None

        for page_no in range(page_count):
            page = document[page_no]
            page_number = page_no + 1

            text_lines = _lines_with_bbox(page)
            table_boxes = _table_boxes(page)
            has_images = bool(page.get_images())

            # 页面里的文字段落与表格按 y 交错排序，保证阅读顺序
            items: list[tuple[float, Block]] = []
            for block in _paragraph_blocks(text_lines, page_number):
                items.append((block.coords[1] if block.coords else 0.0, block))
            pdf_page = pdfplumber.pages[page_no] if pdfplumber is not None and page_no < len(pdfplumber.pages) else None
            for table_no, bbox in enumerate(table_boxes, start=1):
                table_block = _extract_table(page, bbox, pdf_page, page_number, table_no)
                items.append((bbox[1], table_block))

            items.sort(key=lambda item: item[0])
            blocks.extend(block for _y, block in items)

            # 探针路由：没有文字也没有表格
            if not text_lines and not table_boxes:
                if has_images:
                    blocks.append(Block(text="", content_type=TYPE_IMAGE, page=page_number))
                    ocr_pages.append(page_number)
                    notes.append(f"第 {page_number} 页没有文字层，待 OCR")
                else:
                    notes.append(f"第 {page_number} 页是空白")

        if pdfplumber is not None:
            pdfplumber.close()
        document.close()
    finally:
        _cleanup_pdf(pdfplumber_path)

    if not blocks:
        notes.append("这份 PDF 里没有文字层，可能是扫描件——要 OCR 才能读")

    return ParsedDocument(
        blocks=blocks,
        pages=page_count,
        notes=notes,
        ocr_pages=ocr_pages,
        table_count=sum(1 for block in blocks if block.content_type == TYPE_TABLE),
    )


def _table_boxes(page) -> list[tuple[float, float, float, float]]:
    """fitz 检测表格区域（只给 bbox；单元格由 pdfplumber 抠）。"""
    try:
        tables = page.find_tables().tables
        return [tuple(float(v) for v in table.bbox) for table in tables if table.bbox]
    except Exception as exc:
        logger.warning("find_tables 失败（page {}）：{}", page.number + 1, exc)
        return []


def _stage_pdf(data: bytes) -> Path | None:
    """把 PDF 字节落成临时文件（pdfplumber 要路径；不用 mkdtemp，受限令牌下不可访问）。"""
    try:
        folder = paths.tmp_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"pdfparse-{uuid.uuid4().hex[:8]}.pdf"
        target.write_bytes(data)
        return target
    except OSError as exc:
        logger.warning("写 PDF 临时文件失败（退回纯 fitz）：{}", exc)
        return None


def _cleanup_pdf(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("清理 PDF 临时文件失败：{}", exc)
