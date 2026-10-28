"""解析的**数据形状**：块（Block）+ 文档（ParsedDocument）。

## 在流水线里的位置

本模块是解析层与下游之间的**契约**，不干活、只定义形状：

- **上游**：各格式解析器（:mod:`app.core.parse.text` / ``docx`` / ``pdf`` / ``xlsx`` /
  ``pptx``）按结构单元造 :class:`Block` 与 :class:`Unit`，交给 :mod:`app.core.parse`
  统一清洗与聚合；
- **本模块**：规定"一个块能带哪些定位与结构字段、怎么写进 JSON、怎么读回来"；
- **下游**：结构识别填 ``level``/``section_path``；切分（:mod:`app.core.chunk`）把块切成
  parent/child 并把定位带进 ``ChunkRecord``（序列化口径见 :func:`loc_payload`）。
  历史 SQLite 里它落在 ``chunks.loc_json``；当前 Qdrant payload 只带 ``page`` /
  ``section_path`` / ``level``（见 :mod:`app.rag.store.chunks`）——引用跳转读的就是它。

## 输入输出

- 输入：解析器手里已有的原文、页码、行号、bbox、样式（字号/加粗）、表格身份。
- 输出：纯 dataclass（无 IO、无第三方依赖），以及两个序列化函数
  （:func:`loc_payload` 写入、:func:`loc_from_json` / :func:`path_from_json` 读出）。

**为什么是"有序块列表"而不是一段文本**（P1.1 的核心）：引用要回答"哪个文件、哪一页、
哪一行（xlsx 多表时哪张表）"，还要能跳回原文位置——这些必须由**解析阶段**就记录下来，
一路带到 chunk，而不是事后从切出来的片段反推。

每个块都带定位信息（``loc`` 那一组字段）：

- ``page`` + ``page_to`` + ``line_from/to``：PDF 页内行号（跨页续接时 page≠page_to）、txt/md 的文件行号；
- ``table_ref``：表格身份（``sheet:名称#序号`` / ``tbl#body序号`` / ``page:1#2``）；
- ``row_from/to``：表格行区间；
- ``para_index``：docx 正文段落的 body 顺序；
- ``coords``：PDF 页内 bbox（渲染高亮用）。

**流式口径**：解析按**结构单元**产出（PDF 一页、txt/md 一段、docx 一个 body 元素、
xlsx 一个表格段、pptx 一张幻灯片），见 :class:`Unit`——一次只有一个单元在内存里，
不用固定字节块（那会切断句子）。

结构字段（``level`` / ``font_size`` / ``bold`` / ``section_path``）由 P1.2 填，
P1.1 先带着原料。``content_type`` 在 P1.1 就定：``text / table / heading / image``。
图片页是"待 OCR"占位（OCR 本体是 P5，接缝在这里留好）。

**只读约定**：:class:`Block` / :class:`Heading` 是 ``frozen`` 的，下游要在其上改字段
（比如结构识别填 ``section_path``）一律用 ``dataclasses.replace`` 造新对象——这样
"解析出来的块"与"加工后的块"不会互相污染，排查时能确定手里这份是哪个阶段的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class ParseError(RuntimeError):
    """解析失败（原因是要给用户看的一句话，界面/任务 note 直接用）。

    为什么单独一个类型：解析层的失败原因分很多种（缺依赖、加密、格式不符、文件损坏），
    但**对用户都是同一件事**——"这份文件读不了，原因是 X"。任务层靠这个类型把
    "可预期的失败"（记一条 note、跳过并继续）与"代码 bug"（应当冒泡报警）分开。
    """


#: 块类型：正文 / 表格（整块保留）/ 标题（P1.2 标）/ 图片（待 OCR 占位）
TYPE_TEXT = "text"
TYPE_TABLE = "table"
TYPE_HEADING = "heading"
TYPE_IMAGE = "image"
TYPE_CODE = "code"
#: 作者/单位（文档开头、短、人名或含单位字样）：**独立成块**，不并进正文
TYPE_BYLINE = "byline"

#: 定位方式：文件行 / 页内行 / 段落 / 表格（存进 loc_json 用）
#: 优先级见 :attr:`Block.loc_kind`：表格 > 段落 > 页 > 行（一个块只报一种"主定位"）。
LOC_LINES = "lines"
LOC_PAGE = "page"
LOC_PARA = "para"
LOC_TABLE = "table"


def loc_payload(
    *,
    kind: str | None = None,
    page: int | None = None,
    page_to: int | None = None,
    line_from: int | None = None,
    line_to: int | None = None,
    table_ref: str | None = None,
    row_from: int | None = None,
    row_to: int | None = None,
    col_from: int | None = None,
    col_to: int | None = None,
    header: tuple[str, ...] | None = None,
    para_index: int | None = None,
    coords: tuple[float, float, float, float] | None = None,
) -> dict[str, object]:
    """定位信息的**唯一序列化口径**（历史写进 ``chunks.loc_json``，见模块说明）。

    为什么抽成模块函数：块（解析期）与记录（切分期）都要写这个 JSON，
    两处各写一份必然漂移——引用跳转读的就是它，漂移了就是"点引用跳错地方"。

    **只写非 None 的键**：``loc_json`` 是"有什么定位就写什么"，缺键=这项没有；
    写 ``null`` 会让读端要区分"没有"与"有但是空"，白白多一种状态。

    参数（都是可选，缺省即"没有这项定位"）：
        kind: 主定位方式（:data:`LOC_LINES` / :data:`LOC_PAGE` / :data:`LOC_PARA` /
            :data:`LOC_TABLE`）；空串/None 不写。
        page: 起始页码（1 基）。
        page_to: 结束页码；与 ``page`` 相同表示同页（调用方通常只在**跨页续接**时才传）。
        line_from: 行号起（1 基；文件行号或页内行号，由 ``kind`` 决定怎么解释）。
        line_to: 行号止（含）。
        table_ref: 表格身份的字符串（``sheet:名称#序号`` 等）。
        row_from: 表格行区间起。
        row_to: 表格行区间止。
        para_index: docx body 顺序下标（0 基）。
        coords: ``(x0, y0, x1, y1)`` 的 bbox（PDF 页内坐标）。

    返回：
        可直接 ``json.dumps`` 的 dict。**注意键名是 ``table`` / ``para``**（不是字段名
        ``table_ref`` / ``para_index``）：这是写进库的既成口径，读写两端都按它，不要各改各的。
    """
    payload: dict[str, object] = {}
    if kind:
        payload["kind"] = kind
    for name, value in (
        ("page", page),
        ("page_to", page_to),
        ("line_from", line_from),
        ("line_to", line_to),
        ("table", table_ref),
        ("row_from", row_from),
        ("row_to", row_to),
        ("col_from", col_from),
        ("col_to", col_to),
        ("para", para_index),
        ("coords", coords),
    ):
        if value is not None:
            payload[name] = value
    if header is not None and header:
        payload["header"] = list(header)
    return payload


@dataclass(frozen=True)
class Block:
    """文档里的一个有序块（原文片段 + 定位 + 结构原料）。

    "块"是解析层最小的**语义单元**：一个段落、一张整表、一个代码块、一张图占位。
    切分器保证 child 不跨块（见 :mod:`app.core.chunk`），所以块的边界就是
    "允许切开"的边界——解析时把段分对，切分时就不会把一句话劈两半。
    """

    text: str
    """块的原文（**清洗后**的版本，见 :mod:`app.core.parse.clean`）。

    多数格式会顺手 ``strip()`` 掉首尾空白（段落/表格/标题）；**代码块保留原样**
    （缩进和结尾换行有意义）。表格块里是渲染好的文本（行用 ``\\n``、单元格用 `` | ``
    分隔），不是原始二维数据——下游只消费文本。图片占位块是空串。
    """

    content_type: str = TYPE_TEXT
    """块类型：:data:`TYPE_TEXT` / :data:`TYPE_TABLE` / :data:`TYPE_HEADING` /
    :data:`TYPE_IMAGE` / :data:`TYPE_CODE` / :data:`TYPE_BYLINE`。
    下游靠它区分"这是正文、结构还是不能切的原子块"。"""

    # ---- 定位（引用跳转的原料）----
    page: int | None = None
    """页码（PDF/PPT 有；txt/md 无）。1 基。"""

    page_to: int | None = None
    """结束页。**跨页续接**时与 ``page`` 不同（页尾句子未完 → 与下一页首块合并），
    引用显示成"第 5–6 页"、如实说明跨了两页；None 表示与 ``page`` 同页。"""

    line_from: int | None = None
    """文件或页内的行号起（1 基）。"""

    line_to: int | None = None
    """行号止（含）。"""

    table_ref: str | None = None
    """表格身份：``sheet:名称#序号`` / ``tbl#序号`` / ``page:页号#序号``。"""

    row_from: int | None = None
    """表格行区间起（1 基）。

    **基准随格式而变**：docx/pptx/md/csv 是"表内第几行"（从 1 数）；
    xlsx 是 **sheet 绝对行号**（第一行数据在第 7 行就是 7）。展示时要按格式解释，
    见各解析器的说明。
    """

    row_to: int | None = None
    """表格行区间止（含）；基准同 ``row_from``。"""

    col_from: int | None = None
    """表格列区间起（1 基，列字母序）。用于"一行按列拆块"时标明列范围。"""

    col_to: int | None = None
    """表格列区间止（含）。"""

    header: tuple[str, ...] = ()
    """表头单元格（跨列展开后，1 基列序）。展示引用时用它渲染小表。"""

    table_title: str = ""
    """表格标题（显式表名/caption）；会追加到 section_path 末尾，不覆盖章节链。"""

    merged_ranges: tuple[tuple[int, int, int, int], ...] = ()
    """合并单元格 ``(row_from, row_to, col_from, col_to)``（1 基，含端点）。"""

    para_index: int | None = None
    """docx 正文段落的 body 顺序下标（0 基）。"""

    coords: tuple[float, float, float, float] | None = None
    """PDF 页内 bbox ``(x0, y0, x1, y1)``（高亮用）。整块的并集。"""

    line_coords: tuple[tuple[float, float, float, float], ...] = ()
    """**每一行**的 bbox（设计上 PDF 才有；顺序与块文本的行一一对应）。

    为什么单列一个字段：块级 ``coords`` 是整段并集，引用高亮会把 20 行段落全框上；
    切分时按行号映射到这里的子区间，才能**只框住 child 真正覆盖的那几行**。
    **不序列化**（只活在解析→切分这一段），``loc_json`` 里存的是算好的子区间并集。

    现状：当前 :mod:`app.core.parse.pdf` **还没填这个字段**（默认空元组），所以切分侧
    :func:`app.core.chunk.loc._coords_span` 会退回块级 ``coords``（整段框住）。
    """

    # ---- 结构（P1.2 填；P1.1 只带原料）----
    level: int | None = None
    """标题层级 1–4。

    对**标题块**是"它是第几级标题"；对**正文块**是"它所属章节的深度"（见
    :meth:`app.core.parse.structure.Structurer.feed`）——所以正文块的 level 通常不是 None，
    不要用它判断"这块是不是标题"（那要看 ``content_type``）。
    """

    font_size: float | None = None
    """字号（磅），结构识别启发式的原料。

    只有解析器能拿到**显式**字号时才填（PDF 逐 span、docx 逐 run）；样式继承来的字号
    拿不到，留 None——硬猜一个默认值会把基准字号带偏。
    """

    bold: bool = False
    """是否加粗（任一 run/span 加粗即为 True）。只作"有编号时的佐证"，不单独判标题。"""

    doc_title_hint: bool = False
    """解析器已确认"这块就是文档标题"（如 HTML 的 ``<title>`` / 首个 ``<h1>``）。

    绝大多数格式靠结构识别按长度/编号猜文档大标题（见
    :func:`app.core.parse.structure._looks_like_doc_title`）；但 HTML 的 ``<title>`` 是
    站点显式给出的标题，不该被"标题不能超过 40 字"这类防误判规则否掉——否则长标题
    又会被退回文件名兜底。置 True 时结构识别直接认作文档大标题。
    **不序列化**（只活在解析→结构识别这一段）。
    """

    section_path: list[str] = field(default_factory=list)
    """标题链（P1.2 填），如 ``["第二章", "2.3 违约"]``。

    正文块是"包住它的那些标题"；标题块**含自己**（最后一项就是它）。没有标题结构时是
    空列表（不是 None）——下游可以无条件遍历。
    """

    @property
    def loc_kind(self) -> str | None:
        """这份块属于哪种定位（存进 loc_json）。

        判定是**优先级**、不是"全都能报"：一个块可能同时有页码和行号（PDF），
        但引用跳转只需要一个主锚点。顺序是 表格 > 段落 > 页 > 行：

        - 表格块：锚点是表身份（``table_ref``），行号是表内行，不能当页内行用；
        - docx：锚点是 body 段号（``para_index``）；
        - PDF/PPT：锚点是页码；
        - txt/md：没有页，退回文件行号。

        返回：
            :data:`LOC_TABLE` / :data:`LOC_PARA` / :data:`LOC_PAGE` / :data:`LOC_LINES`；
            什么定位都没有（比如解析器在内存里拼出来的块）时返回 None。
        """
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
        """序列化定位信息（历史 ``chunks.loc_json`` 列的内容；**不参与检索过滤**）。

        只序列化"引用跳转用得上"的字段：``line_coords``（每行 bbox）**故意不写**——
        它只用于切分时算子区间，写完就没用了，写进库会让 loc_json 膨胀几十倍。
        切分时算好的子区间并集放在记录侧的 ``coords`` 里。

        返回：
            可直接 JSON 化的 dict，键名口径见 :func:`loc_payload`。
        """
        return loc_payload(
            kind=self.loc_kind,
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


@dataclass
class Unit:
    """一个**结构单元**的产出（流式解析的单位）。

    PDF 一页 / txt·md 一段（或一批行）/ docx 一个 body 元素 / xlsx 一个表格段 /
    pptx 一张幻灯片。每个格式的解析器产出 ``Iterator[Unit]``，上层切完一个单元就能写库、
    丢掉——**一次只有一个单元在内存里**，这就是"大文件不整包读"的落地方式。

    单元除了块，还捎带**文档级信息**（页数、提示、书签）：它们没有"按块"的归属，
    又必须一路传到 :class:`ParsedDocument`，所以挂在单元上、由上层合并。
    """

    blocks: list[Block] = field(default_factory=list)
    """本单元的有序块（阅读顺序）。图片占位块也算一块（``text`` 为空）。"""

    notes: list[str] = field(default_factory=list)
    """给用户看的提示（空白页、扫描页待 OCR、图片未 OCR 的张数…）。

    由 :func:`app.core.parse.parse_file` 累加进文档；流式路径里由
    ``app.rag.ingest.streaming`` 收进导入结果。**内容是可展示的中文句子**，不是日志。
    """

    pages: int | None = None
    """该格式的总页数（知道的话就带上；不知道留 None）。

    注意是**文档总页数**而不是"本单元第几页"：PDF/PPT 能报准，txt/docx 报不出。
    """

    toc: list[tuple[int, str, int]] | None = None
    """PDF 的**内置书签**：``[(层级, 标题, 页码), ...]``（``get_toc()`` 的原样）。

    只在第一页/首个单元上带一次（它是文档级的东西）。有书签时它就是**原生结构**，
    比字号启发式准得多（P1.2）。
    """


@dataclass(frozen=True)
class Heading:
    """标题树里的一个节点（P1.2 的产出，供核对与展示）。"""

    level: int
    """层级（1–4，超过的已夹到 4）。"""

    title: str
    """标题原文（只做了 ``strip``；编号、标点原样保留）。"""

    path: list[str]
    """从根到**本标题**的标题链（如 ``["第二章", "2.3 违约"]``）。"""

    page: int | None = None
    """标题所在页（PDF 才有）。"""

    line_from: int | None = None
    """标题起始行（PDF 是页内行号，md/txt 是文件行号）。"""

    source: str = "native"
    """层级从哪来：``native``（docx 样式 / md 标记）· ``toc``（PDF 书签）·
    ``heuristic``（字号/加粗/编号兜底）。**留痕是为了排查"这个标题是怎么判出来的"**。"""


@dataclass
class ParsedDocument:
    """一份文件解析出的全部内容（解析 + 结构识别的最终产物）。"""

    blocks: list[Block]
    """**有序**块列表（阅读顺序）；P1.2 之后每块的 ``section_path`` 已填好。"""

    pages: int | None = None
    """总页数（PDF/PPT 报得出；其余为 None）。界面显示进度与摘要用。"""

    notes: list[str] = field(default_factory=list)
    """解析过程中的提示（空白页、待 OCR 页、图片未 OCR 的张数…），**按出现顺序**。"""

    ocr_pages: list[int] = field(default_factory=list)
    """待 OCR 的页号（PDF 无文字层但有图片）。"""

    table_count: int = 0
    """表格块数（摘要展示用；按块类型数，与"几张逻辑表"未必一一对应）。"""

    headings: list[Heading] = field(default_factory=list)
    """标题树（按出现顺序）；**没有标题结构的文档这里是空列表**（P1.2 的"无标题返回空树"）。"""

    toc: list[tuple[int, str, int]] = field(default_factory=list)
    """PDF 内置书签（有就带上，便于核对"用的是书签还是启发式"）。"""

    @property
    def has_structure(self) -> bool:
        """这份文档有没有可用结构（标题树非空）。

        只反映"解析出了标题"，不保证标题都判对——判得准不准看 ``Heading.source``
        （native/toc 可信，heuristic 是兜底）。
        """
        return bool(self.headings)

    @property
    def chars(self) -> int:
        """正文字符数（解析摘要用）。图片占位块是空串，不影响计数。"""
        return sum(len(block.text) for block in self.blocks)

    @property
    def text(self) -> str:
        """把块拼回一段文本（P1.3 之前的临时消费；之后切分直接吃块）。

        块之间用空行连接（块边界=段落边界）；**跳过图片占位块**（空文本，拼进去只会多出
        空行）。注意这份文本**不带定位**——要定位就别用这个属性，直接用 :attr:`blocks`。
        """
        return "\n\n".join(block.text for block in self.blocks if block.content_type != TYPE_IMAGE)

    def locate(self, block: Block) -> str:
        """给人看的一句话定位（引用行展示用），如 ``第 3 页 · 第 12–20 行``。

        跨页续接的块显示 **``第 5–6 页``**（如实说明跨了两页，此时行号跨页无单值，不显示）。

        展示顺序与取舍：

        1. 有页码先说页（PDF/PPT 的主要锚点）；
        2. 表格块只报 ``table_ref``（表身份自带序号，已够定位），**不报行号**——表格的
           "行"是表内行，和页内行号不是一套坐标，一起显示会让人误读；
           （``row_from``/``row_to`` 目前**没有**出现在这句展示里，要显示得扩展这里。）
        3. 非跨页块才报行号（或 docx 的"第 N 段"）。跨页块的行号跨了两套页内坐标，
           单个数字必然错，宁可不报。

        参数：
            block: 任意一个解析出的块（不要求属于本文件，仅用它的定位字段）。
        返回：
            可展示的一句话；什么定位都没有时返回 ``（无定位）``。
        """
        parts: list[str] = []
        if block.page is not None:
            if block.page_to is not None and block.page_to != block.page:
                parts.append(f"第 {block.page}–{block.page_to} 页")
            else:
                parts.append(f"第 {block.page} 页")
        if block.table_ref is not None:
            parts.append(block.table_ref)
            if block.row_from is not None and block.row_to is not None:
                row_text = (
                    f"第 {block.row_from} 行"
                    if block.row_from == block.row_to
                    else f"第 {block.row_from}–{block.row_to} 行"
                )
                if block.col_from is not None and block.col_to is not None:
                    row_text += (
                        f" · 列 {block.col_from}–{block.col_to}"
                        if block.col_from != block.col_to
                        else f" · 列 {block.col_from}"
                    )
                parts.append(row_text)
        elif block.page_to is None or block.page_to == block.page:
            if block.line_from is not None and block.line_to is not None:
                if block.line_from == block.line_to:
                    parts.append(f"第 {block.line_from} 行")
                else:
                    parts.append(f"第 {block.line_from}–{block.line_to} 行")
            elif block.para_index is not None:
                # 段号对用户是 1 基（"第 1 段"），存的是 0 基 body 下标，显示时 +1
                parts.append(f"第 {block.para_index + 1} 段")
        return " · ".join(parts) if parts else "（无定位）"

# ---------------------------------------------------------------------------
# 反序列化（与 loc_payload 配对：写入与读出必须同一套口径）
# ---------------------------------------------------------------------------


def loc_from_json(raw: object) -> dict[str, Any]:
    """把 ``loc_json`` 列读回 dict（**坏 JSON → 空 dict**，不让一条脏数据炸整条链路）。

    三种输入都要容忍：``None``/空串（老数据没这一列）、已经是 dict（调用方内存里的）、
    以及 JSON 文本（从库里读出来的）。**任何解析失败都退化成空 dict**：定位丢了只是
    "引用不能跳转"，而抛异常会让整份文件的展示/重建直接失败——孰轻孰重很明确。

    参数：
        raw: 数据库列值（TEXT / JSON / None），或已经是 dict 的值。
    返回：
        定位 dict；读不出来就是 ``{}``。
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def path_from_json(raw: object) -> list[str]:
    """把 ``section_path`` 列读回标题链（坏 JSON → 空列表）。

    与 :func:`loc_from_json` 同一套容错口径：老数据没有这列、列里是 ``null``、
    或者是坏 JSON，都退化成空列表（"这段不属于任何章节"），绝不抛异常。
    元素统一 ``str()``：库里理论上都是字符串，但手写过/迁移过就不好说了。

    参数：
        raw: 数据库列值，或已经是 list 的值。
    返回：
        标题链；读不出来就是 ``[]``。
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []
