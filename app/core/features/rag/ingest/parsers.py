"""格式解析：文件字节 → 纯文本。

**为什么单独一层**：格式与切分是两件事——解析只管"把内容抠成文字"，切分只管"文字怎么切成片段"。
以后加格式（或者加 OCR）只动这里。

**每个格式一个函数**，签名统一：``(data: bytes, name: str) -> Parsed``。
需要第三方库的格式**在函数内部导入**：装了才支持，没装就报一句人话（而不是整个应用起不来）。

**扫描件（图片型 PDF）现在还没接 OCR**：解析出来是空文本，会明确报"这可能是扫描件"，
而不是当成"成功导入了一份空资料"。
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from loguru import logger

#: 纯文本类后缀（不需要第三方库）
TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json", ".html", ".htm"})

#: 需要第三方解析库的后缀 → 依赖名（给用户看的提示）
LIBRARY_HINTS = {
    ".pdf": "pypdf",
    ".docx": "python-docx",
    ".xlsx": "openpyxl",
    ".xlsm": "openpyxl",
    ".pptx": "python-pptx",
}

#: 明确不支持的后缀（老式二进制 Office / 图片等）→ 给用户一句话
UNSUPPORTED_HINTS = {
    ".doc": "老式 .doc 请另存为 .docx",
    ".xls": "老式 .xls 请另存为 .xlsx",
    ".ppt": "老式 .ppt 请另存为 .pptx",
    ".png": "图片要先走 OCR（还没接）",
    ".jpg": "图片要先走 OCR（还没接）",
    ".jpeg": "图片要先走 OCR（还没接）",
}

#: 一份资料解析出来的东西
@dataclass
class Parsed:
    """解析结果。"""

    text: str
    pages: int | None = None
    """页数（PDF / PPT 有）；没有页概念的格式是 None。"""

    notes: list[str] = field(default_factory=list)
    """给用户看的补充说明（比如"这份 PDF 里没有文字层，可能是扫描件"）。"""


class ParseError(RuntimeError):
    """解析失败（原因是要给用户看的一句话）。"""


class _TagStripper(HTMLParser):
    """把 HTML 变成纯文本（丢掉 script/style，块级标签换成换行）。

    不引第三方 HTML 库：我们只要"能读的文字"，不要 DOM。
    """

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    _SKIP = {"script", "style", "noscript", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _decode(data: bytes) -> str:
    """解码：UTF-8(含 BOM) → GBK → 兜底替换。

    为什么要 GBK：国内机器上"另存为 ANSI"很常见，只按 UTF-8 读会直接抛解码错。
    """
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    logger.warning("文件既不是 UTF-8 也不是 GBK，按 UTF-8 宽松解码（可能有替换字符）")
    return data.decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    """压掉多余空白：连续空行变一个，行尾空格去掉。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_text(data: bytes, name: str) -> Parsed:
    """纯文本类：直接解码（HTML 额外去标签）。"""
    text = _decode(data)
    if name.lower().endswith((".html", ".htm")):
        stripper = _TagStripper()
        stripper.feed(text)
        text = "".join(stripper.parts)
    return Parsed(text=_clean(text))


def _parse_csv(data: bytes, name: str) -> Parsed:
    """CSV/TSV：转成"每行一条、字段用制表符隔开"的文本。

    为什么不用原样文本：逗号分隔的表格当成自然语言切分很糟；按行展开更容易命中。
    """
    text = _decode(data)
    delimiter = "\t" if name.lower().endswith(".tsv") else ","
    rows = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        cells = [cell.strip() for cell in row if cell and cell.strip()]
        if cells:
            rows.append(" | ".join(cells))
    return Parsed(text=_clean("\n".join(rows)))


def _parse_json(data: bytes, name: str) -> Parsed:
    """JSON：拍平成"键: 值"的行，便于检索。"""
    try:
        payload = json.loads(_decode(data))
    except ValueError as exc:
        raise ParseError(f"JSON 格式不对：{exc}") from exc
    lines: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        else:
            lines.append(f"{path}: {node}" if path else str(node))

    walk(payload, "")
    return Parsed(text=_clean("\n".join(lines)))


def _parse_pdf(data: bytes, name: str) -> Parsed:
    """PDF：用 pypdf 抽文字层；抽不到就提示可能是扫描件。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # 没装依赖：说清楚，别让用户以为是文件坏了
        raise ParseError("解析 PDF 需要 pypdf（还没装）：pip install pypdf") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # 很多 PDF 只是"空口令"加密
            except Exception as exc:  # noqa: BLE001 - 允许失败：加密的就只能报错
                raise ParseError("这份 PDF 有口令，打不开") from exc
        pages = [page.extract_text() or "" for page in reader.pages]
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - 解析库对坏文件抛的异常五花八门
        raise ParseError(f"PDF 解析失败：{exc}") from exc

    text = _clean("\n\n".join(pages))
    notes = []
    if not text:
        notes.append("这份 PDF 里没有文字层，可能是扫描件——要 OCR 才能读")
    return Parsed(text=text, pages=len(pages), notes=notes)


def _parse_docx(data: bytes, name: str) -> Parsed:
    """Word（.docx）：段落 + 表格里的文字。"""
    try:
        import docx  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ParseError("解析 Word 需要 python-docx（还没装）：pip install python-docx") from exc

    try:
        document = docx.Document(io.BytesIO(data))
        parts = [para.text for para in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
    except Exception as exc:  # noqa: BLE001 - 同上
        raise ParseError(f"Word 解析失败：{exc}") from exc
    return Parsed(text=_clean("\n".join(parts)))


def _parse_xlsx(data: bytes, name: str) -> Parsed:
    """Excel（.xlsx/.xlsm）：每个工作表逐行展开（取"有值的单元格"，公式取缓存值）。"""
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ParseError("解析 Excel 需要 openpyxl（还没装）：pip install openpyxl") from exc

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 同上
        raise ParseError(f"Excel 解析失败：{exc}") from exc

    lines: list[str] = []
    try:
        for sheet in workbook.worksheets:
            lines.append(f"# {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                if cells:
                    lines.append(" | ".join(cells))
    finally:
        workbook.close()
    return Parsed(text=_clean("\n".join(lines)))


def _parse_pptx(data: bytes, name: str) -> Parsed:
    """PowerPoint（.pptx）：每页的文本框与表格，页码进 notes 之外还给出 pages。"""
    try:
        from pptx import Presentation  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ParseError("解析 PPT 需要 python-pptx（还没装）：pip install python-pptx") from exc

    try:
        presentation = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 同上
        raise ParseError(f"PPT 解析失败：{exc}") from exc

    lines: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        lines.append(f"# 第 {index} 页")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = str(shape.text_frame.text or "").strip()
                if text:
                    lines.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        lines.append(" | ".join(cells))
    return Parsed(text=_clean("\n".join(lines)), pages=len(presentation.slides))


def _looks_like_zip(data: bytes) -> bool:
    """OOXML 文件（docx/xlsx/pptx）本质是 zip；用来识别"后缀骗人"的文件。"""
    return data[:2] == b"PK"


def parse(data: bytes, name: str) -> Parsed:
    """按后缀解析一份文件。

    参数：
        data: 文件字节。
        name: 文件名（只看后缀）。
    返回：:class:`Parsed`。
    异常：
        ParseError: 不支持或有依赖缺失或文件损坏（原因可直接给用户看）。
    """
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix in UNSUPPORTED_HINTS:
        raise ParseError(f"暂不支持这种格式：{UNSUPPORTED_HINTS[suffix]}")
    if suffix in LIBRARY_HINTS and not _looks_like_zip(data) and suffix != ".pdf":
        raise ParseError(f"这个 {suffix} 文件不像是真的 Office 文档（内容不是 zip 包）")

    if suffix in (".csv", ".tsv"):
        return _parse_csv(data, name)
    if suffix == ".json":
        return _parse_json(data, name)
    if suffix in TEXT_SUFFIXES:
        return _parse_text(data, name)
    if suffix == ".pdf":
        return _parse_pdf(data, name)
    if suffix == ".docx":
        return _parse_docx(data, name)
    if suffix in (".xlsx", ".xlsm"):
        return _parse_xlsx(data, name)
    if suffix == ".pptx":
        return _parse_pptx(data, name)

    # 没有后缀、或者没见过的后缀：当纯文本试一把（很多日志/笔记文件就是这样）
    if not suffix:
        return _parse_text(data, name)
    raise ParseError(f"暂不支持这种格式：{suffix}")


def supported_suffixes() -> tuple[str, ...]:
    """界面上"支持哪些格式"的那行字用它生成（避免文档与代码漂移）。"""
    return tuple(sorted(TEXT_SUFFIXES | set(LIBRARY_HINTS)))
