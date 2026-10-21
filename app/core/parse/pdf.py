"""PDF：**fitz 探针/抽文字 + pdfplumber 抠表格**的组合（P1.1 定稿）。

## 在流水线里的位置

- **上游**：:func:`app.core.parse.iter_units` 按 ``.pdf`` 后缀派发到这里；
- **本模块**：逐页读出"这页有什么"（文字行 + 表格 + 图片 + 书签），产出带页内行号、
  bbox、字号/加粗的块；**不判标题**（书签与字号原料交给结构识别）、**不切分**；
- **下游**：统一清洗 → 结构识别（书签优先、字号启发式兜底）→ 切分 → 入库。

## 输入输出

- 输入：``.pdf`` 路径（**必须已落盘**：fitz 按路径打开、按页读，才能一页一页放内存）。
- 输出：``Iterator[Unit]``；文字块带 ``page``/``page_to``/``line_from``/``line_to``/
  ``coords``（整块 bbox）/``font_size``/``bold``；表格块带 ``page:N#序号`` 与行区间；
  图片页/扫描页是 ``TYPE_IMAGE`` 占位块（``text=""``）+ 一条 note。

## 关键约定

- **页码一律 1 基**（fitz 内部是 0 基，出口前 +1）；
- **坐标是 PDF 用户单位**（点），原点在左上（fitz 的约定），高亮直接可用；
- **失败包成 ParseError**：打不开/加密/渲染异常——``str(exc)`` 可直接展示；
- **pdfplumber 是可选的**：装不上/打不开就退回纯 fitz（表格精度差些，但不阻断导入）。

分工（用户拍板：组合才是王道，精准优先、慢可接受）：

- **fitz（PyMuPDF）**：打开（**按路径**，按页读）、逐页探针（文字行带 bbox、图片、表格区域）、
  以及以后渲染原页（引用跳转的图）——C 内核快；
- **pdfplumber**：在 fitz 给的表格区域内精确抠单元格（``crop().extract_table()``），
  表格重建比 fitz 稳。慢，但表格是引用要精确定位的对象。

**流式口径（用户要求：大文件不能一次读太多）**：``iter_units(path)`` 逐页产出——
一次只有一页的文本在内存；切完一页就能写库、丢弃。

**跨页断句（方案 A：解析期续接，用户拍板）**：页尾的块若**不以句末标点结束**（且不太长），
把它暂存为 ``carry``，与下一页首个文字块合并；合并块 ``page``＝起始页、``page_to``＝结束页
（引用显示"第 5–6 页"）。防误合并：尾部超过 200 字不续、下一页首块像标题/编号不续、
不是文字块（表格/图片）不续。**保证：一个块永远不会在句中结束 → 切分器按块切就不会切碎句子。**

**探针路由（每页）**：有文字 → 文字块（页内行号 + 段落合并 + bbox）；有表格 → 表格块
（``page:N#序号`` + 行号）；无文字有图片 → ``content_type='image'`` 占位 + ``ocr_pages`` 记页号
（**OCR 本体是 P5，接缝在这**）；都无 → 空白页 notes。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import fitz
from loguru import logger

from app.core.parse.model import (
    TYPE_IMAGE,
    TYPE_TABLE,
    TYPE_TEXT,
    Block,
    ParseError,
    Unit,
)

#: 句末标点（含收尾的引号/括号）：能结束一句话的字符集。
#: 页尾块若**不以**这些结尾 → 认为句子未完 → 续接到下一页。
_SENTENCE_END = "。！？；.!?;…"  #: 含 ASCII 句号：**英文页尾没有它就不会断开**（实测 3 页英文 PDF 被并成 1 块）
#: 紧跟句末标点之后的收尾符：``他说："……。"`` 的最后是引号，不剥掉就会误判"句子未完"
_SENTENCE_END_TAIL = "」』】）\"'”’"
#: 续接的长度上限：页尾那一块超过 200 字就不接了（多半是整段分栏/脚注，接起来更糟）
_CARRY_MAX_CHARS = 200
#: 字号相差到这个倍数就算两段（标题紧贴正文时靠它分段，见 ``_paragraph_blocks``）
_SIZE_BREAK_RATIO = 1.15
#: 一页的图片覆盖到这个比例、且文字少于此数 → 判为**扫描页**（见 ``iter_units``）
_SCANNED_IMAGE_RATIO = 0.5
_SCANNED_MAX_CHARS = 100
#: 像标题/编号的开头（续接时遇到这些开头就停，别把标题并进上一页的正文）
#: 只列了最常见的：``第X章/节``、``一、``～``十、``、``1.``～``9.``、``（1）``、附件/附录。
#: 宁可漏列（漏了最多多接一段正文，切分时还能切开），不可乱列（误停会把一句话拆成两块）。
_HEADING_LIKE = ("第", "一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、", "九、", "十、",
                 "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "0.", "（1）", "（2）",
                 "(1)", "(2)", "附件", "附录")


def _ends_sentence(text: str) -> bool:
    """这段文字是否以句末标点结束（含收尾引号括号）。

    空/纯空白文本返回 True：**"没有文字"不该被当成"句子未完"**——否则空白页的
    空块会被 carry 下去，接到下一页正文前面。

    参数：
        text: 块的文本。
    返回：
        以句末标点（或"句末标点+收尾符"）结尾返回 True。
    """
    stripped = text.rstrip()
    if not stripped:
        return True  # 空块不算"未完"
    if stripped[-1] in _SENTENCE_END:
        return True
    return (
        stripped[-1] in _SENTENCE_END_TAIL
        and len(stripped) >= 2
        and stripped[-2] in _SENTENCE_END
    )


def _dominant_size(spans: list[dict]) -> float:
    """一行里**按字符数主导**的 span 字号（不是最大值）。

    为什么不用最大值：一行里夹个着重号大字（或反之上标），用 max 会让整行字号跳变，
    进而误触发 :func:`_size_break` 的段落切分，把正文段落切碎（实测踩过）。
    主导 span 是"这一行大部分字用什么字号排的"。

    实现上取 ``max((字符数, 字号))``：字符数相同时按字号大者胜（元组比较），
    这比"先按字符数排序再取第一个"更省一次排序。

    参数：
        spans: fitz ``get_text("dict")`` 里一行的 span 列表（每项含 ``text``/``size``）。
    返回：
        主导 span 的字号；没有非空 span 时返回 0.0（调用方按"未知"处理）。
    """
    weighted = [
        (len(str(span.get("text", "")).strip()), float(span.get("size", 0.0)))
        for span in spans
        if str(span.get("text", "")).strip()
    ]
    return max(weighted)[1] if weighted else 0.0


def _lines_with_bbox(page) -> list[tuple[str, tuple[float, float, float, float], float, bool]]:
    """抽一页的文字行：``(text, bbox, size, bold)``，按阅读顺序。

    ``size`` 取该行**按字符数主导的 span** 的字号（不是最大值）——一行里夹个上标、
    脚注号或局部大字，用 max 会让整行字号跳变，进而误触发下面 `_size_break` 的段落切分
    （实测踩过：正文段落被上标切碎）。``bold`` 取"任一 span 是粗体"
    （PyMuPDF 的 span flags 第 4 位是 bold；字体名里带 Bold 也算）。
    这两个是 P1.2 字号启发式的原料，缺了就没法判标题。

    排序用 ``(y0, x0)``：先上后下、同一行内先左后右。fitz 返回的 block 顺序在
    双栏/表格页会乱，必须自己排——**阅读顺序错了，后面的段落合并和标题判断全错**。
    （已知局限：双栏页按 y 排会把左右栏的行交叉，这是"一维阅读顺序"本身的限制。）

    参数：
        page: fitz 的 ``Page``。
    返回：
        行元组列表；空行（拼接后只含空白）被跳过。**坐标是页面用户单位**。
    """
    lines: list[tuple[str, tuple[float, float, float, float], float, bool]] = []
    raw = page.get_text("dict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            bbox = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
            size = _dominant_size(spans)
            bold = any(
                (int(span.get("flags", 0)) & 16) or "bold" in str(span.get("font", "")).lower()
                for span in spans
            )
            lines.append((text, bbox, size, bold))
    lines.sort(key=lambda item: (item[1][1], item[1][0]))
    return lines


def _paragraph_blocks(
    lines: list[tuple[str, tuple[float, float, float, float], float, bool]],
) -> list[Block]:
    """把一页的文字行并成段落块；行号 = 页内行序（1 基）。

    **段落边界有两个判据**（第一个是不够的，实测踩过）：

    1. **纵向间隔**（行距变大）；
    2. **字号跳变**——标题常常紧贴正文（间隔不够大），只按行距会把它并进上一段，
       段落的"中位数字号"随之退回正文字号，**结构识别就再也看不到这个标题了**
       （实测：13 磅的"一、付款方式"被并进 11 磅正文段）。
       字号是比行距更可靠的段落边界信号。

    间隔阈值取 ``max(8pt, 行高 × 1.6)``：绝对值兜住行高异常小的情况（图注/表格里的
    紧凑行），倍数兜住大字号行——只用固定值会在 20 磅标题后把正文并进来。

    参数：
        lines: :func:`_lines_with_bbox` 的产出（已按阅读顺序排好）。
    返回：
        段落块列表（``content_type`` 全是 text；页号由调用方（``iter_units``）补，
        因为这里只处理一页，不需要知道页号）。
    """
    blocks: list[Block] = []
    current: list[tuple[str, tuple[float, float, float, float], float, bool]] = []
    start_line: int | None = None
    for line_no, item in enumerate(lines, start=1):
        bbox = item[1]
        if not current:
            current.append(item)
            start_line = line_no
            continue
        prev_bbox = current[-1][1]
        # gap > 0 表示两行之间有空隙；相邻行（紧排）gap 是 0 或负数
        gap = bbox[1] - prev_bbox[3]
        threshold = max(8.0, (bbox[3] - bbox[1]) * 1.6)
        if gap <= threshold and not _size_break(current[-1][2], item[2]):
            current.append(item)
        else:
            blocks.append(_close_paragraph(current, start_line, line_no - 1))
            current, start_line = [item], line_no
    if current:
        blocks.append(_close_paragraph(current, start_line, len(lines)))
    return blocks


def _size_break(previous: float, current: float) -> bool:
    """两行字号差得够多 → 段落边界（任一边为 0/未知时不判）。

    用"大/小"的比值而不是差值：字号是相对量，10→12 磅的跨度比 30→32 磅更像换层级。

    参数：
        previous: 上一行的主导字号（磅）。
        current: 当前行的主导字号（磅）。
    返回：
        比值 ≥ ``_SIZE_BREAK_RATIO`` 返回 True；任一为 0/None 返回 False。
    """
    if not previous or not current:
        return False
    ratio = max(previous, current) / min(previous, current)
    return ratio >= _SIZE_BREAK_RATIO


def _close_paragraph(
    lines: list[tuple[str, tuple[float, float, float, float], float, bool]],
    line_from: int,
    line_to: int,
) -> Block:
    """把累积的若干行收成一个段落块（算 bbox 并集、字号中位数、加粗）。

    几个口径：

    - ``text``：行用 ``\\n`` 连接（**保留块内换行**，切分时靠它映射回行号/bbox）；
    - ``coords``：所有行的 bbox **并集**（整块高亮用）；行级精度在 ``line_coords`` 上，
      逐行 bbox 原样带上，引用可以框到具体行而不是整段；
    - ``font_size``：**中位数**而不是均值/首行——段落第一行常是标题或首行缩进的大字，
      均值会被带偏；中位数对"整段基本都是正文字号"更稳；
    - ``bold``：任一行加粗即 True。

    参数：
        lines: 本段的行（``_lines_with_bbox`` 的元素）。
        line_from: 本段起始页内行号（1 基）。
        line_to: 本段结束页内行号（含）。
    返回：
        一个 ``TYPE_TEXT`` 块；``page`` 留空（由调用方统一填）。
    """
    text = "\n".join(item[0] for item in lines)
    xs = [item[1][0] for item in lines] + [item[1][2] for item in lines]
    ys = [item[1][1] for item in lines] + [item[1][3] for item in lines]
    sizes = sorted(item[2] for item in lines if item[2])
    # 段落字号取中位数（第一行常是标题、会把均值带偏；中位数更稳）
    size = sizes[len(sizes) // 2] if sizes else None
    bold = any(item[3] for item in lines)
    return Block(
        text=text,
        content_type=TYPE_TEXT,
        line_from=line_from,
        line_to=line_to,
        coords=(min(xs), min(ys), max(xs), max(ys)),
        # 逐行 bbox：引用高亮可以精确到行，而不是整段一个大框
        line_coords=tuple(item[1] for item in lines),
        font_size=size,
        bold=bold,
    )


def _extract_table(
    page, bbox: tuple[float, float, float, float], pdfplumber_page, page_number: int, table_no: int
) -> Block:
    """在 fitz 给的表格区域内，用 pdfplumber 精确抠单元格。

    为什么不在 fitz 里抠完：fitz 的 ``table.extract()`` 对合并单元格、无框线表格
    经常把两列并成一列；pdfplumber 的基于文字坐标的表重建更稳，代价是慢。
    所以整页只让 fitz 找**区域**（很快），抠内容交给 pdfplumber（只裁这块）。

    兜底顺序：pdfplumber 没装/打不开/这块抠不出 → 退回该 bbox 内的 fitz ``find_tables``
    → 再不行就返回**空文本的表格块**（块还在、定位还在，只是没内容；宁可留空壳，
    也不要让"这里有张表"这个事实消失）。

    参数：
        page: fitz 的 ``Page``（兜底用）。
        bbox: 表格区域 ``(x0, y0, x1, y1)``。
        pdfplumber_page: 对应的 pdfplumber 页对象；None 表示 pdfplumber 不可用。
        page_number: 页码（1 基，只用于日志与 ``table_ref``）。
        table_no: 本页内第几张表（1 基）。
    返回：
        ``TYPE_TABLE`` 块；单元格用 `` | ``、行用 ``\\n`` 渲染。
    """
    rows: list[list[str]] = []
    #: 每**行**的 bbox（pdfplumber 才能给；退回到 fitz 的兜底就没有，引用高亮退到整块）
    row_coords: list[tuple[float, float, float, float]] = []
    try:
        if pdfplumber_page is not None:
            # 只裁表格区域：整页 extract_table 会把版面里的其他文字也吃进来
            cropped = pdfplumber_page.crop((bbox[0], bbox[1], bbox[2], bbox[3]))
            # 用 find_tables 拿**同一个** Table 对象：既取数据、又取逐行 bbox（行高亮）。
            # extract_table() 会丢弃几何信息，不能两全。
            table_candidates = cropped.find_tables()
            if table_candidates:
                table = table_candidates[0]
                extracted = table.extract()
                if extracted:
                    rows = [[(cell or "").strip() for cell in row] for row in extracted if any(row)]
                    row_coords = [tuple(float(v) for v in cell.bbox) for cell in table.rows]
    except Exception as exc:
        logger.warning("pdfplumber 提取表格失败（page {} #{}）：{}，退回 fitz", page_number, table_no, exc)
    if not rows:
        # 兜底也要**限定在这个 bbox 内**：早先写成整页 find_tables()，一页有两张表时
        # 每个表块都会装进整页所有表的内容（实测读码确认的缺陷）。
        try:
            found = page.find_tables(clip=fitz.Rect(bbox))
            rows = [row for table in found.tables for row in table.extract()]
        except Exception:
            rows = []

    # 单元格可能是 None（fitz 的 extract() 对合并单元格/空单元格会返回 None），
    # 直接 join 会 TypeError；这里统一转字符串并丢掉空单元格
    rendered = "\n".join(
        " | ".join(str(cell) for cell in row if cell not in (None, ""))
        for row in rows
        if any(cell not in (None, "") for cell in row)
    )
    header = tuple(str(c) for c in rows[0]) if rows else ()
    ncols = max((len(row) for row in rows), default=0)
    return Block(
        text=rendered,
        content_type=TYPE_TABLE,
        page=page_number,
        page_to=page_number,
        table_ref=f"page:{page_number}#{table_no}",
        row_from=1,
        row_to=max(1, len(rows)),
        col_from=1,
        col_to=max(1, ncols),
        header=header,
        coords=bbox,
        line_coords=tuple(row_coords),
    )


def _image_coverage(page) -> float:
    """页面上最大图片占整页面积的比例（判断"这页其实是扫描图"用）。

    只看**最大的一张**而不是面积总和：扫描页常是"整页一张大图 + 几个装饰小图标"，
    求和会被小图标稀释。取 max 才对得上"这页被一张图盖住了"的直觉。

    任何异常都返回 0.0（探测失败就当没有大图）：这个值只用于"要不要提示用户待 OCR"，
    不该因为它把整份 PDF 的解析搞失败。

    参数：
        page: fitz 的 ``Page``。
    返回：
        0.0–1.0 的比例；拿不到页面尺寸/图片矩形时返回 0.0。
    """
    try:
        area = abs(float(page.rect.width) * float(page.rect.height))
        if area <= 0:
            return 0.0
        best = 0.0
        for info in page.get_images():
            # get_image_rects 给的是这张图在页面上的**实际摆放区域**（可能被旋转/裁剪）
            for rect in page.get_image_rects(info[0]):
                best = max(best, abs(float(rect.width) * float(rect.height)) / area)
        return best
    except Exception as exc:
        logger.debug("图片覆盖面积探测失败：{}", exc)
        return 0.0


def _table_boxes(page) -> list[tuple[float, float, float, float]]:
    """fitz 检测表格区域（只给 bbox；单元格由 pdfplumber 抠）。

    失败（老版本 fitz 无此 API、该页结构异常）返回空列表并记一条 warning：
    "这页可能漏表"是可接受的降级，不该让整份文件解析失败。

    参数：
        page: fitz 的 ``Page``。
    返回：
        表格区域列表（按 fitz 给的顺序，未排序；调用方会按 y 排序）。
    """
    try:
        tables = page.find_tables().tables
        return [tuple(float(v) for v in table.bbox) for table in tables if table.bbox]
    except Exception as exc:
        logger.warning("find_tables 失败（page {}）：{}", page.number + 1, exc)
        return []


def _carry_units(carry: Block, page_count: int) -> Unit:
    """文档末尾还有未续接的 carry → 单独产出一个单元（保留它**原来的页号**）。

    不接也得产出：块已经在上一页从 blocks 里摘掉了，这里不吐出去就等于**丢了一段文字**。

    参数：
        carry: 上一页页尾未完成的块。
        page_count: 文档总页数（给 Unit.pages 用）。
    返回：
        只含这一个块的单元。
    """
    return Unit(blocks=[carry], pages=page_count)


def iter_units(path: Path) -> Iterator[Unit]:
    """**逐页**产出（流式：一次一页在内存；跨页断句在解析期续接）。

    每页的处理顺序（也是读代码的顺序）：

    1. **打开**：fitz 按路径打开；``needs_pass`` 时先试空口令（很多"加密" PDF 只是
       权限位，空口令就能开）。注意这里只捕"抛异常"这一种失败：PyMuPDF 的
       ``authenticate`` 在口令不对时可能返回 0 而不抛，此时要到后面取页才会炸
       （已知粗糙处：这里没检查返回值）；
    2. **抽探针**：文字行（``_lines_with_bbox``）、表格区域（``_table_boxes``）、
       有没有图片（``page.get_images()``）；
    3. **并段落 + 排阅读顺序**：文字行先并成段落块，再和表格块**按 y 坐标交错排序**
       （页面上表格可能夹在段落中间，不排序会把整页表格挪到文末）；
    4. **补页号**：文字块统一填 ``page``/``page_to``（本页起止），表格块在
       ``_extract_table`` 里已经填好；
    5. **探针路由**：无文字无表格 → 图片占位块（记 ocr note）或空白页 note；
       整页大图 + 零星文字 → 也补一个图片占位块并提示"按扫描页处理"（文字仍然保留）；
    6. **跨页续接**：上一页的 carry 先尝试与**本页首个文字块**合并（不成就插到本页最前）；
       本页末尾文字块若句子未完且不太长，摘下来留给下一页；
    7. **产出单元**：``blocks`` 或 ``notes`` 非空才产出（整页什么都没有时只在 notes 里
       留一句，不至于让"第 5 页是空白"这种事实消失）。

    **书签只带一次**：``toc`` 是文档级信息，挂在第一个产出的单元上（``yield`` 后置 None），
    避免每个单元都背一份。

    **扫描页不留 carry**：扫描页的文字多半是页眉/页脚，把它接到下一页正文前面，
    等于给正文塞了一句无关的话。

    参数：
        path: ``.pdf`` 文件路径（fitz 需要真实路径，不能是流）。
    返回：
        逐页的单元流；页号 1 基。
    异常：
        ParseError: 文件打不开、需要口令且空口令失败。
    """
    try:
        document = fitz.open(str(path))
    except Exception as exc:
        raise ParseError(f"打不开这份 PDF（可能损坏）：{exc}") from exc

    if document.needs_pass:
        try:
            opened = document.authenticate("")  # 很多 PDF 只是"空口令"加密
        except Exception as exc:
            document.close()
            raise ParseError(f"这份 PDF 有口令，打不开：{exc}") from exc
        if not opened:
            # PyMuPDF 在口令不对时可能**返回 0 而不抛异常**；不检查的话要到取页才炸，
            # 且报的不是 ParseError（用户看到的会是一句底层错误）
            document.close()
            raise ParseError("这份 PDF 需要打开口令（暂不支持输入密码），请先解密后再导入")

    page_count = document.page_count
    pdfplumber = None
    try:
        try:
            import pdfplumber

            # pdfplumber 只是"表格更准"的增强：打不开就退回纯 fitz，不阻断导入
            pdfplumber = pdfplumber.open(str(path))
        except Exception as exc:
            logger.warning("pdfplumber 打不开这份 PDF（退回纯 fitz）：{}", exc)
            pdfplumber = None

        carry: Block | None = None  # 跨页续接的暂存块（方案 A）
        toc: list[tuple[int, str, int]] | None = None
        try:
            raw_toc = document.get_toc()  # 内置书签：P1.2 的**原生结构**
            if raw_toc:
                toc = [(int(item[0]), str(item[1]), int(item[2])) for item in raw_toc]
        except Exception as exc:
            # 书签读不出来不是错误：退回字号启发式即可（结构识别那边本来就支持无书签）
            logger.debug("读 PDF 书签失败（退回启发式）：{}", exc)

        for page_no in range(page_count):
            page = document[page_no]
            page_number = page_no + 1

            text_lines = _lines_with_bbox(page)
            table_boxes = _table_boxes(page)
            has_images = bool(page.get_images())

            # 页面里的文字段落与表格按 y 交错排序（阅读顺序）
            items: list[tuple[float, Block]] = []
            for block in _paragraph_blocks(text_lines):
                items.append((block.coords[1] if block.coords else 0.0, block))
            # 当前页的 pdfplumber 页对象（只取这一页；越界时给 None，走 fitz 兜底）
            pdf_page = pdfplumber.pages[page_no] if pdfplumber is not None and page_no < len(pdfplumber.pages) else None
            for table_no, bbox in enumerate(table_boxes, start=1):
                items.append((bbox[1], _extract_table(page, bbox, pdf_page, page_number, table_no)))
            items.sort(key=lambda item: item[0])

            blocks: list[Block] = []
            notes: list[str] = []

            for _y, block in items:
                if block.content_type == TYPE_TABLE:
                    # 表格块在 _extract_table 里已经带齐页号/表身份，原样收下
                    blocks.append(block)
                    continue
                # 文字块在这里才补页号：_paragraph_blocks 只处理单页，不知道自己是第几页
                blocks.append(
                    Block(
                        text=block.text,
                        content_type=block.content_type,
                        page=page_number,
                        page_to=page_number,
                        line_from=block.line_from,
                        line_to=block.line_to,
                        coords=block.coords,
                        font_size=block.font_size,  # P1.2 启发式的原料
                        bold=block.bold,
                    )
                )

            # 探针路由：没有文字也没有表格 → 图片页/空白页
            if not text_lines and not table_boxes:
                if has_images:
                    blocks.append(Block(text="", content_type=TYPE_IMAGE, page=page_number, page_to=page_number))
                    notes.append(f"第 {page_number} 页没有文字层，待 OCR")
                else:
                    notes.append(f"第 {page_number} 页是空白")

            # **扫描页**：整页大图 + 只有零星文字（典型：扫描件带一行页眉/页脚）。
            # 早先只要有一行文字就当文字页，于是扫描内容**静默丢失、连提示都没有**
            # （用户的第一原则是精准，宁可标出来待 OCR，也不能假装读到了）。
            # 注意这里**不删已有文字块**：页眉文字本身也可能是有效信息，只是不可信。
            total_chars = sum(len(text) for text, *_rest in text_lines)
            scanned = (
                has_images
                and not table_boxes
                and _image_coverage(page) >= _SCANNED_IMAGE_RATIO
                and total_chars < _SCANNED_MAX_CHARS
            )
            if scanned and text_lines:
                blocks.append(Block(text="", content_type=TYPE_IMAGE, page=page_number, page_to=page_number))
                notes.append(
                    f"第 {page_number} 页几乎全是图片（只认出 {total_chars} 个字），按扫描页处理，待 OCR"
                )

            # ---- 跨页续接（方案 A）----
            if carry is not None:
                # 只跟"本页第一个文字块"合并（不是最后一个）：carry 是上一页的句尾，
                # 它后面紧接着的应该是本页页首的正文，而不是页尾的东西。
                first_text_index = next(
                    (i for i, block in enumerate(blocks) if block.content_type == TYPE_TEXT),
                    None,
                )
                if first_text_index is not None and _can_merge(carry, blocks[first_text_index]):
                    merged = _merge_carry(carry, blocks[first_text_index])
                    blocks[first_text_index] = merged
                else:
                    # 接不上（本页没文字/首块像标题/是表格）→ 原样插在最前；
                    # 它自己的 page 还是上一页，定位仍然对
                    blocks.insert(0, carry)  # 保留它原来的页号（就是上一页）
                carry = None

            # 本页尾的文字块若句子未完 → 留作 carry。
            # **扫描页不留 carry**：那点文字多半是页眉页脚，把它接到下一页正文上更糟。
            last_index = next(
                (
                    index
                    for index in range(len(blocks) - 1, -1, -1)
                    if blocks[index].content_type == TYPE_TEXT
                ),
                None,
            )
            if (
                not scanned
                and last_index is not None
                and not _ends_sentence(blocks[last_index].text)
                and len(blocks[last_index].text) <= _CARRY_MAX_CHARS
            ):
                # **按下标删**，不按文本相等删：同一页若有内容完全相同的两块，
                # ``blocks.remove(text)`` 可能删掉前面那一块。
                carry = blocks.pop(last_index)

            if blocks or notes:
                # 书签是文档级的：只在第一个产出的单元上带一次
                yield Unit(blocks=blocks, notes=notes, pages=page_count, toc=toc)
                toc = None

        if carry is not None:
            # 最后一页的页尾句子没写完（文档就这么结束了）：单独产出，别丢
            yield _carry_units(carry, page_count)

    finally:
        # 无论中途抛什么（含生成器被提前 close），句柄都要释放；
        # pdfplumber 的 close 出问题也不能盖住真正的异常，所以 suppress
        if pdfplumber is not None:
            with contextlib.suppress(Exception):
                pdfplumber.close()
        document.close()


def _can_merge(_carry: Block, next_block: Block) -> bool:
    """续接条件：下一块是文字、不是标题/编号开头。

    ``_carry`` 参数**不参与判断**（前缀只说了"下一块"）：它留着是为了让调用点读起来
    对称、也给将来"按 carry 的结尾语气决定接不接"留位置；下划线前缀告诉 ruff 这是
    有意不用的参数。

    为什么"像标题/编号"就不接：``第 5 条``、``一、``、``附件`` 这些开头几乎总是新段落，
    把上一页的残句接上去会造出一个假段落（而且切分器会把它当成一个块）。

    参数：
        _carry: 上一页未完成的块（不参与判断，见上）。
        next_block: 本页第一个文字块。
    返回：
        可以合并返回 True。
    """
    if next_block.content_type != TYPE_TEXT:
        return False
    text = next_block.text.lstrip()
    if not text:
        return False
    return not text.startswith(_HEADING_LIKE)


def _merge_carry(carry: Block, next_block: Block) -> Block:
    """把 carry（上一页未写完的句子）与下一页首块合并成一个块。

    loc：``page``＝起始页、``page_to``＝结束页、行号跨页各记一段。

    行号只记 ``line_from``（起始页的首行）与 ``line_to``（结束页的末行）**两个端点的行号**，
    中间跨了几行无法用一对行号表达——这正是 ``model.ParsedDocument.locate`` 在跨页时
    **不显示行号**的原因。

    用空格连接（而不是直接拼）：中文不需要，但英文续接必须留词边界；中文里多一个空格
    不影响检索（分词会忽略）。

    **样式沿用 carry**（字号/加粗取起始块）：续接块的主体是上一页那句话，
    P1.2 判标题时按它的样式更合理；``coords`` 也只保留起始页那块（高亮框在起始页）。

    参数：
        carry: 上一页页尾的块。
        next_block: 本页页首的文字块。
    返回：
        合并后的新块（``page``/``page_to`` 不同 → 引用显示"第 N–M 页"）。
    """
    text = carry.text.rstrip() + " " + next_block.text.strip()
    return Block(
        text=text,
        content_type=TYPE_TEXT,
        page=carry.page,
        page_to=next_block.page,
        line_from=carry.line_from,
        line_to=next_block.line_to,
        coords=carry.coords,
        font_size=carry.font_size,  # 续接块沿用起始块的样式（P1.2 判标题用）
        bold=carry.bold,
    )
