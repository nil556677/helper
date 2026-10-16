"""解析：文件字节 → **有序块列表 + 定位信息**（P1.1）。

```python
parsed = parse(data, "合同.pdf")
for block in parsed.blocks:
    print(block.text, block.page, block.line_from, block.table_ref)
```

每个格式用最成熟的单一库（P1.1 定稿）：

| 格式 | 库 | 关键点 |
| --- | --- | --- |
| txt/log | stdlib | UTF-8/GBK，块带文件行号 |
| md | markdown-it-py | 原生结构：标题层级、表格、行号 |
| csv/tsv | stdlib | 整份一张表（整块保留） |
| json | stdlib | 拍平成行（无行号可言） |
| html | stdlib HTMLParser | 去标签成文本 |
| pdf | **fitz + pdfplumber** | fitz 探针/文字/渲染，pdfplumber 抠表格，图片页待 OCR |
| docx | python-docx | body 顺序 + 原生样式层级 |
| xlsx | openpyxl | 按空行切多表，绝对行号 |
| pptx | python-pptx | 页码 = 幻灯片序号 |

**统一流水线**（每个文件都走）：后缀路由 → 格式嗅探（防"后缀骗人"）→ 分类型提取 →
块化归一（带 loc）→（P1.2 结构识别在这里之后接）。
"""

from __future__ import annotations

from app.core.parse import docx, pdf, pptx, text, xlsx
from app.core.parse.model import (
    TYPE_CODE,
    TYPE_HEADING,
    TYPE_IMAGE,
    TYPE_TABLE,
    TYPE_TEXT,
    Block,
    ParsedDocument,
    ParseError,
)

#: 需要第三方解析库的后缀 → 库名（给用户看的提示，装不了/坏了时用）
LIBRARY_HINTS = {
    ".pdf": "PyMuPDF + pdfplumber",
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


def _looks_like_zip(data: bytes) -> bool:
    """OOXML 文件（docx/xlsx/pptx）本质是 zip；用来识别"后缀骗人"的文件。"""
    return data[:2] == b"PK"


def _looks_like_pdf(data: bytes) -> bool:
    """PDF 的魔数是 ``%PDF``（识别"后缀骗人"的文件）。"""
    return data[:5] == b"%PDF-"


def parse(data: bytes, name: str) -> ParsedDocument:
    """按后缀解析一份文件。

    参数：
        data: 文件字节。
        name: 文件名（只看后缀）。
    返回：:class:`ParsedDocument`（有序块 + 定位信息 + 元数据）。
    异常：
        ParseError: 不支持 / 缺依赖 / 文件损坏（原因可直接给用户看）。
    """
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix in UNSUPPORTED_HINTS:
        raise ParseError(f"暂不支持这种格式：{UNSUPPORTED_HINTS[suffix]}")

    # 格式嗅探：后缀与内容不一致 → 明确拒绝（别拿错解析器硬啃）
    if suffix in LIBRARY_HINTS:
        if suffix == ".pdf" and not _looks_like_pdf(data):
            raise ParseError("这个 .pdf 文件开头不是 %PDF，不像真的 PDF 文档")
        if suffix != ".pdf" and not _looks_like_zip(data):
            raise ParseError(f"这个 {suffix} 文件不像是真的 Office 文档（内容不是 zip 包）")

    if suffix in (".csv", ".tsv", ".json", ".md", ".markdown", ".txt", ".log", ".html", ".htm") or not suffix:
        return text.parse(data, name)
    if suffix == ".pdf":
        return pdf.parse(data, name)
    if suffix == ".docx":
        return docx.parse(data, name)
    if suffix in (".xlsx", ".xlsm"):
        return xlsx.parse(data, name)
    if suffix == ".pptx":
        return pptx.parse(data, name)
    raise ParseError(f"暂不支持这种格式：{suffix}")


def supported_suffixes() -> tuple[str, ...]:
    """界面上"支持哪些格式"那行字（从清单生成，避免文档与代码漂移）。"""
    return tuple(sorted(set(text.TEXT_SUFFIXES) | set(LIBRARY_HINTS)))


__all__ = [
    "TYPE_CODE",
    "TYPE_HEADING",
    "TYPE_IMAGE",
    "TYPE_TABLE",
    "TYPE_TEXT",
    "Block",
    "ParsedDocument",
    "parse",
    "supported_suffixes",
]
