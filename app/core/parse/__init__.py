"""解析（P1.1）：文件 → **有序块列表 + 定位信息**。

## 在流水线里的位置

整条导入链路是「**解析 → 结构识别 → 切分 → 入库**」，本包只做第一段，且**只做第一段**：

- **上游**：调用方（:mod:`app.ingest.pipeline`、任务层、测试）给一个**已落盘**的路径
  （导入时是 inbox 里的 ``{uuid}_{原名}``），外加可选的**展示用文件名**；
- **本包**：按后缀分派到各格式解析器，产出「结构单元」:class:`Unit`（内含有序
  :class:`Block` 与定位原料），并统一清洗不可见字符。**不切分、不算 token、不写库**；
- **下游**：结构识别（:mod:`app.core.parse.structure`，补 ``section_path``/``level``）
  → 切分（:mod:`app.core.chunk`）→ 入库。定位字段一路带到 chunk，引用跳转才有依据。

## 输入输出

- 输入：文件路径 + 可选展示名；或 ``bytes``（:func:`parse`，先落临时目录再走同一条路）。
- 输出：``Iterator[Unit]``（流式，:func:`iter_units`）或整份 :class:`ParsedDocument`
  （:func:`parse_file`，已跑过结构识别）。块里只有"原文 + 定位"，没有向量、没有预算。

## 关键约定

1. **格式嗅探在前**：后缀与文件头对不上（``.pdf`` 不是 ``%PDF-``、Office 不是 zip 包）、
   或不支持的后缀，直接 :class:`ParseError`——宁可明确拒绝，也不拿错解析器硬啃出乱码；
2. **流式按结构单元**，不用固定字节块（见下）；
3. **展示名与路径名分开**：``name`` 只影响引用里露出的文件名，不参与打开文件
   （inbox 的路径带 ``{uuid}_`` 前缀，直接用它会把暂存前缀写进 ``table_ref``）；
4. **失败一律包成 :class:`ParseError`**（缺依赖 / 加密 / 损坏）：``str(exc)`` 是可以
   直接展示给用户的一句话，任务层不用再猜。

三种入口（**一条代码路径**，格式嗅探在前）：

```python
iter_blocks(path)            # 流式：逐结构单元产出（大文件用这个，一次一个单元在内存）
parse_file(path)             # 一次性：整份 ParsedDocument（小文件/测试/预览用）
parse(data, name)            # bytes 版：临时落盘后走 parse_file（上传前的字节 / 测试）
```

**流式口径（用户要求：大文件不能一次读太多）**：每个格式按**结构单元**产出——
PDF 一页（含跨页断句续接）、txt/log 一段（增量解码防跨块拆字）、docx 一个 body 元素、
xlsx 一个表格段、pptx 一张幻灯片。**不用固定字节块做文本处理**（那会切断句子）；
1 MB 字节块只用于**字节搬运 + 增量解码**（见 :data:`app.core.parse.text.STREAM_CHUNK`），
不参与分段，与切分无关。

| 格式 | 库 | 关键点 |
| --- | --- | --- |
| txt/log | stdlib（增量解码） | 流式，块带文件行号 |
| md | markdown-it-py | 原生结构：标题层级/表格/行号 |
| csv/tsv | stdlib | 整份一张表（整块保留） |
| html | stdlib HTMLParser | 去标签成文本 |
| pdf | **fitz + pdfplumber** | fitz 探针/文字/渲染，pdfplumber 抠表格，图片页待 OCR，**跨页断句续接** |
| docx | python-docx | body 顺序 + 原生样式层级 |
| xlsx | openpyxl | 按空行切多表，绝对行号 |
| pptx | python-pptx | 页码 = 幻灯片序号 |
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from app.core import paths
from app.core.parse import docx, pdf, pptx, structure, text, xlsx
from app.core.parse.clean import strip_invisible
from app.core.parse.model import (
    TYPE_CODE,
    TYPE_HEADING,
    TYPE_IMAGE,
    TYPE_TABLE,
    TYPE_TEXT,
    Block,
    Heading,
    ParsedDocument,
    ParseError,
    Unit,
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
    # JSON 是结构化数据：字段语义不明确，按文本解析没有检索价值（且无法判断哪个字段是正文）。
    # 需要入库时请转成表格（csv/xlsx）或正文（txt/md/docx）再导入。
    ".json": "JSON 是结构化数据，字段含义不明确，暂不解析；请转成 CSV/XLSX 或正文后导入",
    ".png": "图片要先走 OCR（还没接）",
    ".jpg": "图片要先走 OCR（还没接）",
    ".jpeg": "图片要先走 OCR（还没接）",
}

#: 纯文本类后缀的别名（``supported_suffixes`` 与界面共用，避免两处各写一份清单）
_TEXT_SUFFIXES = text.TEXT_SUFFIXES


def _suffix_of(name: str) -> str:
    """取**小写后缀**（含点）；没有后缀返回空串。

    只取**最后一段**后缀（语义与 ``Path.suffix`` 一致），但**不依赖文件名是合法路径**：
    ``name`` 是展示名，可能来自用户输入、带非法字符，不该为取个后缀去构造 ``Path``。
    没有后缀的文件不报错——交给 :func:`_raw_units` 当纯文本处理（见那里的分派）。
    """
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def _sniff(path: Path) -> None:
    """格式嗅探：后缀与内容不一致 → 明确拒绝（别拿错解析器硬啃）。

    为什么必须做：用户把 ``.docx`` 改名成 ``.pdf``、下载到半截的 Office 文件、网盘占位文件
    都很常见；交给 fitz/python-docx 硬读，轻则报一句看不懂的底层异常，重则解析出**乱码正文**
    并悄悄入库（这比失败更糟——用户以为搜得到）。

    规则：只比对**文件头前几个字节**（PDF 看 ``%PDF-``，其余 Office 看 zip 包的 ``PK``）；
    不支持的后缀走 :data:`UNSUPPORTED_HINTS`，给"请另存为 .docx"这类**能直接照做**的建议。
    实现只 ``read(8)`` 读头部，不把整份文件读进来（大 PDF 不该为 8 字节多付一次全量 IO）。

    参数：
        path: 待解析文件（必须已存在；调用方负责落盘）。
    异常：
        ParseError: 后缀不支持、文件读不了、或文件头与后缀不符。
    """
    suffix = _suffix_of(path.name)
    if suffix in UNSUPPORTED_HINTS:
        raise ParseError(f"暂不支持这种格式：{UNSUPPORTED_HINTS[suffix]}")
    # 纯文本/未知后缀不做文件头校验：它们没有魔数，硬校验只会误杀（UTF-16/GBK 都可能）
    if suffix not in LIBRARY_HINTS:
        return
    try:
        # 只读文件头 8 字节：不要 read_bytes() 整份（大 PDF 会白白多一次全量 IO）
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError as exc:
        raise ParseError(f"读文件失败：{exc}") from exc
    if suffix == ".pdf":
        if head[:5] != b"%PDF-":
            raise ParseError("这个 .pdf 文件开头不是 %PDF，不像真的 PDF 文档")
    elif head[:2] != b"PK":
        raise ParseError(f"这个 {suffix} 文件不像是真的 Office 文档（内容不是 zip 包）")


def iter_units(path: Path, name: str | None = None) -> Iterator[Unit]:
    """**流式**按结构单元产出块（大文件的入口；一次一个单元在内存）。

    ``name``：**展示用文件名**（默认取 ``path.name``）。导入时文件先落在 inbox
    （``{uuid}_{原名}``），传原始名才能让引用里的表身份是 ``csv:清单.csv#1``
    而不是带暂存前缀的东西。

    **所有格式的块都过一遍不可见字符清洗**（:func:`app.core.parse.clean.strip_invisible`）——
    这是唯一的出口，散在各格式里必然漂移。

    参数：
        path: 磁盘上的文件（已落盘；按后缀嗅探 + 分派）。
        name: 展示用文件名；影响**格式路由**与引用里露出的名字，不参与打开文件。
    返回：``Iterator[Unit]``，顺序即阅读顺序；每个单元内部块也有序。
    异常：
        ParseError: 见 :func:`_sniff`；解析器内部的失败也都已包成 ParseError。
    """
    # 先嗅探再分派：假后缀在这里就被拦住，不会进到某个解析器里炸出底层异常
    _sniff(path)
    suffix = _suffix_of(name or path.name)
    for unit in _raw_units(path, suffix, name):
        yield _clean_unit(unit)


def _raw_units(path: Path, suffix: str, name: str | None) -> Iterator[Unit]:
    """按后缀分派到各格式的解析器（**未清洗**的原始单元）。

    分派表与模块 docstring 的格式表一一对应（``.xlsm`` 与 ``.xlsx`` 同一支）；
    **没有后缀**的当成纯文本（用户改名的日志、无扩展名导出文件都很常见，
    报"不支持"不如按文本读）。``name`` 只透传给需要它的解析器（csv/tsv 的
    ``table_ref`` 用展示名）。
    """
    # 文本类走同一个解析器：内部再按后缀细分（csv/md/html/txt 的处理各不相同）
    if suffix in (".csv", ".tsv", ".md", ".markdown", ".txt", ".log", ".html", ".htm") or not suffix:
        yield from text.iter_units(path, name=name)
    elif suffix == ".pdf":
        yield from pdf.iter_units(path)
    elif suffix == ".docx":
        yield from docx.iter_units(path)
    elif suffix in (".xlsx", ".xlsm"):
        yield from xlsx.iter_units(path)
    elif suffix == ".pptx":
        yield from pptx.iter_units(path)
    else:
        # 有后缀但没进上面任何一支：说明是没接过的格式（.pages/.wps/.odt …）
        raise ParseError(f"暂不支持这种格式：{suffix}")


def _clean_unit(unit: Unit) -> Unit:
    """清洗单元里每个块的文本（行号/bbox 不受影响：清洗不动换行、也不动行结构）。

    顺序：先除不可见噪声，再做**统一归一**（全半角/中文数字/日期/空白）——
    与查询侧同一套：func:`app.rag.normalize.normalize`，保证库内与查询 token/embedding 一致。
    用 ``replace`` 造新块而不是原地改：:class:`Block` 是 frozen 的，且同一块对象可能
    被上层持有；造新对象能保证"清洗前/后"两份不会互相污染。
    """
    from app.rag.normalize import normalize  # 入库侧统一归一（与查询同源）

    blocks = [replace(block, text=normalize(strip_invisible(block.text))) for block in unit.blocks]
    return replace(unit, blocks=blocks)


def iter_blocks(path: Path, name: str | None = None) -> Iterator[Block]:
    """把 :func:`iter_units` 摊平（按阅读顺序的块流）。

    单元边界信息（``Unit.pages`` / ``notes`` / ``toc``）在这里**被丢掉**；
    需要页面数、提示或书签的调用方应该直接用 :func:`iter_units` 或 :func:`parse_file`。
    """
    for unit in iter_units(path, name=name):
        yield from unit.blocks


def parse_file(path: Path, name: str | None = None) -> ParsedDocument:
    """解析磁盘上的一个文件 → 整份 :class:`ParsedDocument`（小文件/测试/预览用）。

    **产出前会跑一遍结构识别（P1.2）**：每块的 ``section_path`` 与文档的 ``headings``
    都已填好（无标题的文档 headings 为空、section_path 为空列表）。
    ``name`` 是展示用文件名（见表身份里的文件名）。

    这里做的是**整份聚合**：把每个单元里的块按序收拢，并把单元级的文档信息（页数、
    提示、书签）合并成文档级字段。注意聚合口径：

    - ``pages`` 取**最后一个**非 None 的单元值（各解析器报的是文档总页数，重复覆盖无妨）；
    - ``notes`` **按单元顺序累加并保留重复**（不同页的提示各自有意义，去重会丢页号）；
    - ``toc`` 取**最后一个非空值**（PDF 解析器只在首个单元带一次，所以实际就是那一份）；
    - ``ocr_pages`` 从图片占位块的 ``page`` 推出并去重排序——没有页号的图片块（docx）
      不进这个列表。

    参数：
        path: 磁盘上的文件（已落盘）。
        name: 展示用文件名。
    返回：结构识别后的 :class:`ParsedDocument`。
    异常：
        ParseError: 同 :func:`iter_units`。
    """
    blocks: list[Block] = []
    notes: list[str] = []
    ocr_pages: list[int] = []
    pages: int | None = None
    toc: list[tuple[int, str, int]] = []
    for unit in iter_units(path, name=name):
        blocks.extend(unit.blocks)
        notes.extend(unit.notes)
        if unit.pages is not None:
            pages = unit.pages
        if unit.toc:
            toc = list(unit.toc)
        # 只有带页号的图片占位块才算"待 OCR 页"（docx 的图片没有页码，记不了页）
        ocr_pages.extend(block.page for block in unit.blocks if block.content_type == TYPE_IMAGE and block.page)

    parsed = ParsedDocument(
        blocks=blocks,
        pages=pages,
        notes=notes,
        ocr_pages=sorted(set(ocr_pages)),
        table_count=sum(1 for block in blocks if block.content_type == TYPE_TABLE),
        toc=toc,
    )
    # 结构识别放在最后：它需要**整份块列表**先算正文基准字号（流式路径另有运行众数兜底）
    return structure.apply(parsed)


def parse(data: bytes, name: str) -> ParsedDocument:
    """按文件名解析一份**字节**（测试/小文件/上传前的字节；临时落盘后走 parse_file）。

    参数：
        data: 文件字节。
        name: 文件名（后缀决定路由）。
    返回：:class:`ParsedDocument`。
    异常：
        ParseError: 不支持 / 缺依赖 / 文件损坏（原因可直接给用户看）。
    """
    if not data:
        raise ParseError("文件是空的")
    # 嗅探用字节（和 parse_file 的嗅探规则一致，省一次落盘）：
    # 后面 parse_file 还会再嗅探一遍，这里是**提前拒绝**，避免为用户给错的格式白落盘。
    suffix = _suffix_of(name)
    if suffix in UNSUPPORTED_HINTS:
        raise ParseError(f"暂不支持这种格式：{UNSUPPORTED_HINTS[suffix]}")
    if suffix == ".pdf" and data[:5] != b"%PDF-":
        raise ParseError("这个 .pdf 文件开头不是 %PDF，不像真的 PDF 文档")
    if suffix in LIBRARY_HINTS and suffix != ".pdf" and data[:2] != b"PK":
        raise ParseError(f"这个 {suffix} 文件不像是真的 Office 文档（内容不是 zip 包）")

    folder = paths.tmp_dir()
    # 用 uuid 作**目录名**、保留原文件名：path.name 就是原始名
    # （csv 的 table_ref 会显示"清单.csv"而不是"parse-xxx-清单.csv"），后缀也对。
    staged_dir = folder / uuid.uuid4().hex[:8]
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged = staged_dir / Path(name).name
    staged.write_bytes(data)
    try:
        return parse_file(staged, name=name)
    finally:
        # 无论成败都清掉临时文件；目录只有这一份文件时顺手删掉
        # （rmdir 失败说明有并发写入，忽略即可——tmp 目录由清理任务兜底）
        staged.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            staged_dir.rmdir()


def supported_suffixes() -> tuple[str, ...]:
    """界面上"支持哪些格式"那行字（从清单生成，避免文档与代码漂移）。"""
    return tuple(sorted(set(_TEXT_SUFFIXES) | set(LIBRARY_HINTS)))


def outline(parsed: ParsedDocument) -> str:
    """:mod:`app.core.parse.structure` 的 ``outline`` 转出来（核对标题树用）。

    参数：
        parsed: 已跑过结构识别的文档。
    返回：缩进的标题树文本；没有标题时是空串。
    """
    return structure.outline(parsed)


__all__ = [
    "TYPE_CODE",
    "TYPE_HEADING",
    "TYPE_IMAGE",
    "TYPE_TABLE",
    "TYPE_TEXT",
    "Block",
    "Heading",
    "ParseError",
    "ParsedDocument",
    "Unit",
    "iter_blocks",
    "iter_units",
    "outline",
    "parse",
    "parse_file",
    "structure",
    "supported_suffixes",
]
