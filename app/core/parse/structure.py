"""结构识别（P1.2）：块列表 → **标题树 + 每块的 section_path**。

## 在流水线里的位置

「解析 → **结构识别** → 切分 → 入库」的第二段，**介于解析与切分之间**：

- **上游**：解析层给的块（已清洗，带页码/行号/字号/加粗等"原料"）；
- **本模块**：只读块、只填结构字段（``level`` / ``section_path`` / ``content_type`` 的
  heading 化），产出**新的**块列表 + 标题树。**不改文本、不改定位、不切分**；
- **下游**：切分器按标题开新 parent、按 ``section_path`` 记章节；引用展示读它。

## 输入输出

- 输入：``list[Block]``（或整份 :class:`ParsedDocument`，见 :func:`apply`）。
- 输出：带 ``section_path`` 的块 + :class:`Heading` 列表（:meth:`Structurer.headings`）。
  没有标题的文档：标题树为空、所有块 ``section_path`` 为空列表——**这是合法结果，不是失败**。

## 两种调用方式（同一套逻辑）

- **整份**：:func:`apply` 先算全文档正文基准字号，再一次 ``feed``——小文件/测试走这条；
- **流式**：:class:`Structurer` 直接逐单元 ``feed``（``app.rag.ingest.streaming`` 走这条）。
  此时没有"全文档基准"，只能用**运行众数**（每来一个正文块就重算一次，见 ``_sample``）。

**优先级**（用户拍板："原生结构优先，启发式只兜底"）：

| 来源 | 谁有 | 准度 |
| --- | --- | --- |
| ``native`` | docx 的 ``Heading N``/``outlineLevel``、md 的 ``#`` 标记 | 文档自己存的结构，最准 |
| ``toc`` | PDF 的**内置书签**（``get_toc()``） | 有书签就照它分页映射 |
| ``heuristic`` | 没有上面两种时的兜底（PDF 无书签、怪文档） | 编号模式 + 字号跳变 + 加粗 + 短行/无句号 |

**启发式的三条判据**（"像标题的文本"是门票，样式与编号二选一；宁可少判、不可错判——
错判会把正文切碎）：

1. **不长得像正文**：短行（≤ 60 字）且**不以句末标点结尾**；
2. **像标题的样式**：字号 ≥ 正文基准 × 1.15（分三档：1.6 / 1.35 / 1.15）；
3. **编号能定层级时优先用编号**（``第一章`` → H1、``一、`` → H2、``（一）`` → H3、``1.1`` → H4），
   编号与样式冲突时**以编号为准**（编号是作者显式写的层级）。

**加粗不单独作数**：只有"有编号 + 加粗"或"有编号 + 大字号"才判标题；光有编号而字号与正文
相同的，多半是正文里引用条款号（"见第 3 条"），一律不判。原因见 :func:`size_level`。

**正文基准字号**取所有文字块字号的**众数**（全文档先算一遍 → 准；流式时用运行众数兜底）。
层级统一夹在 H1–H4（计划定的上限）。

**树怎么建**：按出现顺序压栈——遇到 Hn 就弹到栈顶层级 < n，再把 Hn 压进去；
每个块的 ``section_path`` = 栈里的标题链（**标题块含自己**，正文块是包住它的那些标题）。
没有标题的文档：树为空、所有块 ``section_path`` 为空列表（"无标题返回空树"）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace

from app.core.parse.model import (
    TYPE_BYLINE,
    TYPE_HEADING,
    TYPE_TEXT,
    Block,
    Heading,
    ParsedDocument,
)

#: 标题层级上限（H1–H4；超过的一律夹到 4）
MAX_LEVEL = 4

#: 编号模式 → 层级（**顺序有意义**：先匹配到的先用）。
#: ``1.1`` 这类多级编号必须排在 ``1、`` 前面——否则 "1.1 违约金的计算方式" 会被
#: 单级编号规则先吃掉、层级判浅（实测踩过）。
_NUMBERED: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*[章篇编部卷]"), 1),
    (re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*[节条]"), 2),
    (re.compile(r"^[一二三四五六七八九十]{1,3}\s*[、.．]"), 2),
    (re.compile(r"^[（(]\s*[一二三四五六七八九十]{1,3}\s*[）)]"), 3),
    (re.compile(r"^\d{1,2}(\.\d{1,2}){1,3}\s*[、.．]?\s*\S"), 4),
    (re.compile(r"^\d{1,2}\s*[、.．]\s*\S"), 3),
)

#: 字号倍数 → 层级（越大越像大标题）。基准是正文众数字号。
_SIZE_TIERS: tuple[tuple[float, int], ...] = ((1.6, 1), (1.35, 2), (1.15, 3))

#: 标题不会太长（比这长的多半是正文段落）
_MAX_HEADING_CHARS = 60

#: 标题一般不以句末标点结尾（以它结尾的多半是正文）
_SENTENCE_END = "。！？；!?;…"

#: 作者/单位行：必须很短，且不能是这些"前置信息"（摘要、关键词、分类号…）
_BYLINE_MAX_CHARS = 60
_BYLINE_NON_BYLINE_PREFIX = (
    "摘要",
    "关键词",
    "abstract",
    "keywords",
    "中图分类号",
    "文献标识码",
    "doi",
    "收稿日期",
    "作者简介",
    # 文档开头常见、但**不是作者**的短行（尤其文档没有标题时）
    "目录",
    "前言",
    "序言",
    "引言",
    "绪论",
    "参考文献",
    "致谢",
    "附录",
    "索引",
)
#: 出现这些字样，基本可以确定是单位行
_BYLINE_UNIT_MARKERS = (
    "大学",
    "学院",
    "研究所",
    "研究院",
    "公司",
    "集团",
    "中心",
    "实验室",
    "医院",
    "银行",
    "事务所",
    "学会",
    "协会",
)

#: 公文文种：出现在开头时是**文档标题/主送**，绝不能当作者行（"关于…的通知"最典型）
_BYLINE_OFFICIAL_WORDS = (
    "通知",
    "公告",
    "通报",
    "请示",
    "报告",
    "批复",
    "函",
    "纪要",
    "意见",
    "决定",
    "命令",
    "公报",
    "决议",
)


def _looks_like_doc_title(text: str) -> bool:
    """"开头第一个 H1"是否够格当**文档大标题**（而不是"第1章 绪论"这种真章节）。

    防的是整份结构错位：把第一章当大标题吞掉后，第一章正文的 ``section_path`` 为空，
    后面所有章节的层级也跟着乱。所以宁可不认（退化为普通标题），也不要错认。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 40:
        return False
    if numbered_level(stripped) is not None:
        return False
    if stripped.endswith(("：", ":", "。", "，", ",")):
        return False
    return stripped not in {"摘要", "关键词", "目录", "参考文献", "附录", "致谢", "前言"}


def looks_like_byline(text: str) -> bool:
    """文档开头的**作者/单位**行：短、不以句末标点结尾、含单位字样或就是一个人名。

    为什么单列一种块类型：作者/单位若混进正文，切分时会和紧跟的"摘要"粘成一段，
    引用里就出现"赵六 XX大学 摘要：…"这种半截内容。识别成 :data:`TYPE_BYLINE` 后
    它自己成块，下游可以单独展示或跳过。

    判据（**宁少不多**：误判会把正常正文/公文标题标成 byline）：

    1. 长度不超过 60 字，且不以句末标点结尾（否则是句子）；
    2. 不以"摘要/关键词/DOI/作者简介"等**前置信息**开头（它们紧跟作者行，长得很像）；
    3. **不含公文文种**（通知/公告/请示…）——"关于…的通知"是标题不是作者；
    4. **不以冒号结尾**——"各学院、各部门："是主送机关，不是作者；
    5. 含"大学/学院/研究所/公司…"等单位字样 → 直接算；
    6. 否则看"去掉空格和间隔号后是 2–4 个纯汉字"——中文人名（"赵六"、"欧阳修"）的形态。
       英文名/机构缩写识别不了，这是有意的取舍：规则太松会误伤标题和短句。

    参数：
        text: 一个块（或一行）的文本。
    返回：
        像作者/单位行返回 True；空串、超长、像句子、像前置信息都返回 False。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _BYLINE_MAX_CHARS:
        return False
    # 去掉大小写差异：摘要/Abstract/DOI 都可能大写开头
    if stripped.lower().startswith(_BYLINE_NON_BYLINE_PREFIX):
        return False
    if stripped[-1] in _SENTENCE_END or stripped.endswith(("：", ":")):
        return False
    if any(word in stripped for word in _BYLINE_OFFICIAL_WORDS):
        return False
    if any(marker in stripped for marker in _BYLINE_UNIT_MARKERS):
        return True
    # 人名形态：去掉常见分隔符后是 2–4 个汉字（"张三" / "李 四" / "欧阳·修"）
    compact = re.sub(r"[\s·•,，.。()（）]", "", stripped)
    return 2 <= len(compact) <= 4 and all("\u4e00" <= ch <= "\u9fff" for ch in compact)


def numbered_level(text: str) -> int | None:
    """从编号模式推层级（``第一章`` → 1、``一、`` → 2、``（一）`` → 3、``1.1`` → 4）。

    只认**行首**编号（``^``），因为这段文本就是一个块的全文；编号在中间的多半是正文里
    的引用（"见第 3 条"），不能当结构。

    参数：
        text: 块的文本（内部会 ``strip``，容忍 PDF 行首的缩进空格）。
    返回：
        命中的层级（1–4）；没有任何编号模式时返回 None。
    """
    stripped = text.strip()
    for pattern, level in _NUMBERED:
        if pattern.match(stripped):
            return level
    return None


def looks_like_heading_text(text: str) -> bool:
    """像标题的**文本特征**：短、不以句末标点结尾。

    这是所有启发式判据的**前置门**（连原生层级/书签匹配之前也要过）：标题短、
    正文长；标题不带句号、正文带。先过这一关能挡掉绝大部分正文段落。

    多行块的特殊处理：PDF 段落有时会把标题和下文并进一块，这时**只要求第一行够短**；
    但整块超过 60 字仍然不算标题（否则一个长段落只要首行短就会被误判）。

    参数：
        text: 块的文本。
    返回：
        像标题返回 True；空串、超长、以句末标点结尾返回 False。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _MAX_HEADING_CHARS:
        return False
    if "\n" in stripped:
        # 多行块：只有第一行短且整块不长时才可能（PDF 段落有时会把标题并进来）
        first = stripped.split("\n", 1)[0]
        if len(first) > _MAX_HEADING_CHARS:
            return False
    return stripped[-1] not in _SENTENCE_END


def body_size(blocks: list[Block]) -> float | None:
    """正文基准字号 = 各字号按**字符数加权**的众数（**标题样的短行不计入**）。

    为什么按字符数而不是"块数"：标题短、正文长——按块数统计时"1 个标题块 vs 1 个正文块"
    会打平，众数可能选中**标题字号**（实测：18 磅标题 + 一个 11 磅正文块 → 基准变 18，
    于是标题不再是标题）。按字符加权等于问"这份文档大部分字是用几号字排的"。

    为什么排除标题样短行：封面大标题会把基准抬高，正文（11 磅）反而"比基准小"，
    后续正常标题就判不出来了。若整份文档全是标题样短行（没有正文可选），
    退回"不排除"的口径，免得基准为空导致一个标题都判不出来。

    字号先按 0.5 磅取整（``round(size * 2) / 2``）：PDF 的 span 字号常带浮点尾巴
    （11.0001 / 10.9998），不归一会把同一个字号拆成好几桶，众数就散了。

    参数：
        blocks: 整份文档的块（只用 ``content_type`` 为文本且带 ``font_size`` 的）。
    返回：
        基准字号（磅）；拿不到任何字号时返回 None（调用方据此关闭字号启发式）。
    """
    weights: Counter[float] = Counter()
    fallback: Counter[float] = Counter()
    for block in blocks:
        if block.content_type != TYPE_TEXT or not block.font_size:
            continue
        bucket = round(float(block.font_size) * 2) / 2
        # 至少算 1：短块（"第 3 条"）也有投票权，但不至于被空文本放大
        weight = max(1, len(block.text))
        fallback[bucket] += weight
        if not looks_like_heading_text(block.text):
            weights[bucket] += weight
    chosen = weights or fallback
    if not chosen:
        return None
    return float(chosen.most_common(1)[0][0])


def size_level(font_size: float | None, _bold: bool, baseline: float | None) -> int | None:
    """按**字号**（相对基准）推层级；字号不满足就返回 None。

    **不再把"只有加粗"当标题**：实测（公文/论文）里加粗大量用于强调关键词、
    列表项的首词（"结构化解析:…"），把它们当标题会切出一堆假章节和只有标题的碎块。
    加粗只作为"有编号时的佐证"（见 ``Structurer._level_of``）。

    ``_bold`` 参数是**故意保留但不再使用**的：调用点都按三参数传，签名保持稳定
    （也提醒读代码的人"这里曾经用加粗判过标题，别再加回来"）。下划线前缀同时告诉
    ruff 这是有意不用的参数。

    分档：≥ 基准 1.6 倍 → H1，≥ 1.35 → H2，≥ 1.15 → H3；不到 1.15 返回 None
    （和正文一样大，不可能是标题）。本函数**不会返回 H4**——H4 来自 ``1.1`` 这类
    多级编号（:func:`numbered_level`）或原生层级（docx/md 自己标的）。

    参数：
        font_size: 块的字号（磅）；None 或 0 表示未知。
        _bold: 是否加粗（不再参与判定）。
        baseline: 正文基准字号；None 表示无法判断（关闭字号启发式）。
    返回：
        层级 1–3；不满足任一分档返回 None。
    """
    if font_size and baseline:
        ratio = float(font_size) / float(baseline)
        for threshold, level in _SIZE_TIERS:
            if ratio >= threshold:
                return level
    return None


class Structurer:
    """**可流式**的结构识别器：喂块进去，吐带 ``section_path`` 的块。

    为什么做成有状态的类：``iter_units`` 是逐单元流的，而"这个正文块属于哪个标题"
    要看**前面**出现过什么标题——所以标题栈必须跨单元保留。``parse_file`` 用它做整份处理，
    P1.3 的流式入库可以直接逐单元 ``feed``。

    内部状态（都是"越过块边界"的东西）：

    - ``_stack``：当前标题链（栈顶=最内层标题），决定正文块的 ``section_path``；
    - ``_headings``：已识别出的全部标题（按出现顺序，即最终标题树）；
    - ``_doc_title``：文档大标题（第一个 H1），**不进栈**，见 :meth:`feed`；
    - ``_sizes`` / ``_baseline``：正文字号样本与运行众数（流式时没有全文档统计）；
    - ``_seen`` / ``_saw_body``：判断"还在文档开头"用（作者/单位行只认开头几块）。
    """

    def __init__(
        self,
        *,
        toc: list[tuple[int, str, int]] | None = None,
        baseline_size: float | None = None,
        doc_title_from_h1: bool = True,
    ) -> None:
        """初始化。

        参数：
            toc: PDF 内置书签 ``[(层级, 标题, 页码), ...]``；传了就把"哪页有什么标题"
                建成索引（同一页多个书签按顺序保留）。流式调用可以晚一点拿到书签
                再建一个新实例（``toc`` 传 None 起手，见 ``app.rag.ingest.streaming``）。
            baseline_size: 已知的正文基准字号（整份路径用 :func:`body_size` 先算好）；
                不传则退化为运行时逐块累积众数（:meth:`_sample`）。
            doc_title_from_h1: 是否把"文档开头第一个 1 级标题"当文档大标题。
                **PPT 必须传 False**：它的标题占位符是每页的页标题，不是文档标题；
                否则第一页标题会被吃掉，整份 PPT 的章节树从第二页才开始。
        """
        self._toc = list(toc or [])
        #: 页码 → 该书签页上的 ``(层级, 标题)`` 列表（PDF 书签按页映射）
        self._toc_pages: dict[int, list[tuple[int, str]]] = {}
        for level, title, page in self._toc:
            self._toc_pages.setdefault(int(page), []).append((int(level), str(title)))
        self._baseline = baseline_size
        self._sizes: Counter[float] = Counter()
        self._stack: list[Heading] = []
        self._headings: list[Heading] = []
        #: 文档大标题（第一个 1 级标题）：它是**元数据**，不进 section_path，
        #: 否则"作者/摘要"会挂在大标题下，用户看到的是"标题变成了正文的章节"。
        self._doc_title: str | None = None
        #: 已经见过正文（非标题/非作者行）——之后不再认作者行、不再认文档大标题
        self._saw_body = False
        #: 已处理的块数：作者/单位只认**文档开头几块**
        self._seen = 0
        #: 已认出的作者/单位块数（最多 2 个；再多就说明前面其实是正文）
        self._byline_count = 0
        #: 是否把开头第一个 1 级标题当文档标题（PPT 关闭）
        self._doc_title_from_h1 = bool(doc_title_from_h1)

    @property
    def doc_title(self) -> str:
        """识别到的文档大标题（没有就是空串）。

        返回值刻意用空串而不是 None：调用方（``documents.doc_title`` 列）直接存字符串，
        少一层 None 判断。
        """
        return self._doc_title or ""

    # ---- 对外 ----

    def feed(self, blocks: list[Block]) -> list[Block]:
        """处理一个单元里的块，返回**填好 level/section_path** 的新块列表。

        这是结构识别的**主循环**，每个块按顺序走四条分支（顺序不能换）：

        1. **文档大标题**（文档最开头的 H1）：只记进 ``doc_title``，**不压栈**；
        2. **标题**：压栈，块变 heading；
        3. **作者/单位行**（文档开头 ≤4 块内、正文样、像人名/单位）：标成 byline，
           不压栈、不并进正文；
        4. **正文**：挂到当前栈顶下面（``section_path`` = 栈里的标题链，
           ``level`` = 所属章节深度）。

        为什么分支 1 要在 2 之前、"不压栈"这么重要：大标题若进栈，后面所有正文的
        ``section_path`` 都会以"文档标题"开头，作者/摘要就变成了它的子章节——
        用户看到的标题树是错的，切分还会把整篇挂在同一个 parent 下。

        参数：
            blocks: 一个结构单元里的有序块（可以是整份文档的全部块）。
        返回：
            等长的新块列表；**输入块不被修改**（:class:`Block` 是 frozen 的，用 replace 造新块）。
        """
        result: list[Block] = []
        for block in blocks:
            level, source = self._level_of(block)
            # 先计数再判断：下面几个"文档开头"条件都依赖它（第 N 块，1 基）
            self._seen += 1
            # 文档大标题：**文档最开头**的 1 级标题。它是元数据（doc_title），
            # 不推进标题栈——否则作者/摘要会挂在大标题下，看起来像"标题被存成了正文"。
            if (
                self._doc_title_from_h1
                and level is not None
                and int(level) == 1
                and self._doc_title is None
                and not self._headings
                and not self._saw_body
                and (block.doc_title_hint or _looks_like_doc_title(block.text))
            ):
                self._doc_title = block.text.strip()
                result.append(
                    replace(
                        block,
                        content_type=TYPE_HEADING,
                        level=1,
                        section_path=[],
                    )
                )
                continue
            if level is not None:
                heading = self._push(block, level, source)
                result.append(
                    replace(
                        block,
                        # 判成标题就**统一成 heading 类型**（不管依据是原生、书签还是启发式）：
                        # 下游（P1.3 切分、引用展示）要靠 content_type 区分"这是结构，不是正文"
                        content_type=TYPE_HEADING,
                        level=heading.level,
                        section_path=list(heading.path),
                    )
                )
                continue
            # 作者/单位行（文档开头几块、短、人名或含单位字样）：**独立成块**，
            # 不并进正文（否则"赵六 + 单位 + 摘要"会粘成一段）。
            # 条件看着啰嗦，每一条都在挡一种误判：前面出现过标题/正文就说明已经过了
            # 作者区（"作者简介"那类段落不能被当成 byline）。
            if (
                level is None
                and block.content_type == TYPE_TEXT
                and self._seen <= 4
                and not self._saw_body
                and not self._headings
                and self._byline_count < 2
                and looks_like_byline(block.text)
            ):
                self._byline_count += 1
                result.append(
                    replace(
                        block,
                        content_type=TYPE_BYLINE,
                        section_path=[],
                        level=None,
                    )
                )
                continue
            self._saw_body = True
            result.append(
                replace(
                    block,
                    section_path=self._current_path(),
                    # 正文块的 level = **它所属章节的深度**（不是 None）：
                    # "只搜某一级章节内的内容"要靠它，展示"这属于第几级标题下"也靠它。
                    # 没有标题的文档里栈为空 → level 保持 None。
                    level=self._stack[-1].level if self._stack else None,
                )
            )
        return result

    def headings(self) -> list[Heading]:
        """到目前为止识别出的标题树（按出现顺序）。

        返回的是**副本**：调用方可以随便改这个 list，不会影响结构识别器的内部状态
        （文档大标题不在里面——它是元数据，见 :attr:`doc_title`）。
        """
        return list(self._headings)

    # ---- 内部 ----

    def _level_of(self, block: Block) -> tuple[int | None, str]:
        """判断这个块是不是标题、是第几级、依据是什么。

        按优先级依次尝试（**前面的命中就返回**，后面的不再看）：

        ① **原生层级**（``block.level``：docx 样式 / md 的 ``#``）——文档自己存的结构，
        最可信。这里**不调** ``_sample``：原生标题块本来就会被 :meth:`_sample` 的
        "只采正文 / 只采文本块"两道过滤挡掉，调了只是白跑；
        ② **PDF 书签**（``_toc_pages`` 里有这一页的条目且文本对得上）；
        ③ **启发式**：编号给层级、字号给层级，两者都有时**以编号为准**
        （编号是作者显式写的层级，字号只是排版）；**只有加粗不算**，而"有编号但没样式
        佐证"也不判——那是正文里引用条款号（"见第 3 条"）的常见形态，宁可漏判。
        表格/图片/代码块一律返回 None：它们是内容，不是结构——哪怕文字很短。

        参数：
            block: 一个块。
        返回：
            ``(层级 或 None, 依据)``；依据是 ``native`` / ``toc`` / ``heuristic`` / ``none``。
            ``none`` 表示"不是标题"，只用于调用方分支，不会写进 :class:`Heading`。
        """
        # ① 原生层级（docx 样式 / md 标记）
        if block.level is not None:
            return max(1, min(MAX_LEVEL, int(block.level))), "native"
        # 表格/图片/代码块不参与标题判定（它们是内容，不是结构）
        if block.content_type != TYPE_TEXT:
            return None, "none"
        text = block.text.strip()
        if not looks_like_heading_text(text):
            # 不像标题的正文块在这里进基准样本（标题样短行不进，见 _sample 的说明）
            self._sample(block)
            return None, "none"

        # ② PDF 书签：这一页有书签条目，且文本与标题对得上
        # （同一页可能有多个书签，所以是遍历而不是取第一个）
        if block.page is not None and self._toc_pages.get(int(block.page)):
            for level, title in self._toc_pages[int(block.page)]:
                if _title_matches(title, text):
                    return max(1, min(MAX_LEVEL, level)), "toc"

        # ③ 启发式：编号 / 字号 / 加粗（标题候选不进基准，见 _sample）
        numbered = numbered_level(text)
        styled = size_level(block.font_size, block.bold, self._baseline)
        if numbered is not None and styled is not None:
            return numbered, "heuristic"  # 编号与样式都像标题：**以编号为准**（作者显式写的层级）
        if numbered is not None and block.bold:
            # 有编号 + 加粗：编号给层级，加粗只当"它确实被作者强调过"的佐证
            return numbered, "heuristic"
        if styled is not None:
            # 有样式没编号：字号大的按字号给级（numbered 必为 None，`or` 只是表达意图）
            return (numbered or styled), "heuristic"
        return None, "none"

    def _sample(self, block: Block) -> None:
        """累积**正文**字号样本并更新基准。

        三个要点（都是实测逼出来的）：

        - **按字符数加权**：标题短、正文长；按"块数"统计会在"1 个标题块 vs 1 个正文块"
          打平时选错字号；
        - **只采正文样**：标题样的短行不进基准，否则文档开头的封面大标题会把基准
          定到标题字号；
        - **每次重算**，而不是"首个样本定终身"——实测：首个块是大字号标题时，
          基准被冻在标题字号，后面 14 磅的真实标题（无编号、不加粗）永远漏判。

        这就是流式路径（没有全文档统计）的兜底：块来得越多，基准越接近全文档众数。
        只在"不像标题"的正文块上调用（见 :meth:`_level_of`）。

        参数：
            block: 一个非标题的文本块；没有字号或不是文本类型时直接忽略。
        """
        if block.content_type != TYPE_TEXT or not block.font_size:
            return
        if looks_like_heading_text(block.text):
            return  # 标题样的不进基准：基准问的是"这份文档正文用什么字号"
        # 与 body_size 同一套 0.5 磅分桶口径（两处必须一致，否则整份/流式结论会不同）
        self._sizes[round(float(block.font_size) * 2) / 2] += max(1, len(block.text))
        if self._sizes:
            self._baseline = float(self._sizes.most_common(1)[0][0])

    def _push(self, block: Block, level: int, source: str) -> Heading:
        """把标题压栈（先弹掉层级 ≥ 自己的），返回它的标题节点。

        标题栈的维护就这一条规则：**遇到 Hn，先弹到栈顶层级 < n**，再压入自己。
        这样栈里永远是"当前章节的祖先链"，正文块的 ``section_path`` 直接取栈即可。
        弹掉"≥ 自己"（而不是"＞ 自己"）是关键：同级的第二个标题要替换掉前一个，
        否则同一层的两个标题会同时挂在路径里（"2.1" 下面套 "2.2"）。

        参数：
            block: 被判成标题的块（用它的文本、页码、行号）。
            level: 已夹到 1–4 的层级。
            source: 层级来源（native/toc/heuristic），写进 :class:`Heading` 留痕。
        返回：
            新建并已入栈的 :class:`Heading`；调用方用它的 ``path`` 填块的 ``section_path``。
        """
        while self._stack and self._stack[-1].level >= level:
            self._stack.pop()
        path = [item.title for item in self._stack] + [block.text.strip()]
        heading = Heading(
            level=level,
            title=block.text.strip(),
            path=path,
            page=block.page,
            line_from=block.line_from,
            source=source,
        )
        self._stack.append(heading)
        self._headings.append(heading)
        return heading

    def _current_path(self) -> list[str]:
        """当前标题链（栈里各标题的文本，从根到最内层）。

        返回新 list：直接给块当 ``section_path``，不能把内部栈暴露出去
        （否则下游 append 一下就把栈改了）。
        """
        return [item.title for item in self._stack]


def _title_matches(toc_title: str, block_text: str) -> bool:
    """书签标题与块文本是否对得上（去掉空白后比前缀，容忍断行/编号空格差异）。

    书签里的标题与页面上的文字**很少逐字相同**：书签常带"第一章 …… 5"这类点线/页码，
    页面上的文字又可能被 PDF 拆成多行、或者带排版空格（"第 1 章" vs "第1章"）。
    所以先去掉所有空白再比前缀，而不是要求完全相等。

    两条判据（命中即算对得上）：

    1. 一方是另一方的**前缀**（去掉空白后直接 ``startswith`` 双向比）；
    2. 否则取较短一方的前 8 字比：书签常带点线/页码，页面上又可能断行，8 字足够锁定；
       但这条**至少 4 字**才认——两三个字的前缀太容易撞（"第一条"这种编号在正文里
       到处都是），宁可漏认、退回字号启发式。

    参数：
        toc_title: 书签里的标题文本。
        block_text: 页面上一个块的文本。
    返回：
        对得上返回 True；任一方为空、或前缀太短则 False。
    """
    left = re.sub(r"\s+", "", toc_title)
    right = re.sub(r"\s+", "", block_text)
    if not left or not right:
        return False
    if left.startswith(right) or right.startswith(left):
        return True
    # 取较短的一方的前 8 字做前缀比对（书签常带页码/点线）
    head = min(len(left), len(right), 8)
    return head >= 4 and left[:head] == right[:head]


def apply(parsed: ParsedDocument) -> ParsedDocument:
    """整份文档跑一遍结构识别，返回填好 ``section_path`` 与 ``headings`` 的新文档。

    与流式路径（``Structurer.feed`` 逐单元）的区别只有一点：这里先用
    :func:`body_size` 把**全文档**的正文基准字号算出来，再喂给结构识别器——
    所以整份路径的字号启发式一开始就是准的，不会出现"前几页判不准"。

    只改 ``blocks`` 与 ``headings`` 两个字段，以及块内的结构字段
    （``level`` / ``section_path``，标题块还有 ``content_type``）；``pages`` / ``notes`` /
    ``ocr_pages`` / ``table_count`` / ``toc`` 原样保留（``replace`` 不动其余字段）。

    参数：
        parsed: :func:`app.core.parse.parse_file` 的产出。
    返回：新 :class:`ParsedDocument`（``blocks`` 是填好的新块，``headings`` 是标题树）。
    异常：
        无（纯内存计算；块列表为空时返回 headings 为空的文档）。
    """
    structurer = Structurer(toc=parsed.toc, baseline_size=body_size(parsed.blocks))
    blocks = structurer.feed(parsed.blocks)
    return replace(parsed, blocks=blocks, headings=structurer.headings())


def outline(parsed: ParsedDocument) -> str:
    """把标题树画成缩进文本（核对与排查用；**空树返回空串**）。

    每行形如 ``## 2.3 违约（第 5 页 · 第 12 行 · heuristic）``：``#`` 的个数=层级，
    括号里是定位与来源。**来源很重要**：``native``/``toc`` 可信，``heuristic`` 是猜的，
    排查"标题判错"时先看这一栏。

    参数：
        parsed: 已跑过结构识别的文档。
    返回：
        多行文本（没有标题时是空串，调用方据此显示"没有标题结构"）。
    """
    lines: list[str] = []
    for heading in parsed.headings:
        indent = "  " * max(0, heading.level - 1)
        where = []
        if heading.page is not None:
            where.append(f"第 {heading.page} 页")
        if heading.line_from is not None:
            where.append(f"第 {heading.line_from} 行")
        suffix = f"（{' · '.join(where)} · {heading.source}）" if where else f"（{heading.source}）"
        lines.append(f"{indent}{'#' * heading.level} {heading.title}{suffix}")
    return "\n".join(lines)
