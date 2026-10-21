"""导入与重建共用的共享私有件（避免子模块之间循环 import）。

放这里的都是 ``importer`` / ``rebuilder`` 两个主模块都要用的私件：
``ImportResult``、``_ImportRun``、``_NothingToIndex``、``_fingerprint``、``_stream_into``。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path

from app.core.chunk import Splitter
from app.core.chunk.diagnostics import Diagnostic, DiagnosticSink
from app.core.jobs.model import JobCancelled
from app.core.parse import iter_units
from app.core.parse.model import TYPE_HEADING, TYPE_IMAGE, Block
from app.core.parse.structure import Structurer
from app.rag import store

from .progress import ProgressFn

#: 写入攒批阈值（子块数）：攒够这么多子块才统一嵌入 + 写 Qdrant。
#: 逐组写（约 5~10 个子块一组）时单次 forward 的固定开销占大头，实测吞吐只有大批的 1/2~1/4。
#: 写入攒批：攒到这么多子块就嵌入+写一次。**不要太大**：嵌入是整批同步的，
#: 一批越大，界面越会"先长时间不动、然后一下跳到 70%"。32 个约几秒一批，
#: 进度和 Qdrant 落点都能持续前进；对吞吐影响很小（模型内部本来就是小批）。
WRITE_BATCH_CHILDREN = 32


def _ensure_document_alive(doc_id: int) -> None:
    """资料行还在吗？被删了就立刻取消，别继续往 Qdrant 写孤儿点。

    为什么需要：删除资料和取消任务是两条链路。用户可能**先删除资料、后取消任务**，
    或者删除时任务正在跑。``delete_documents`` 会先请求取消，但取消是协作式的
    （批次/单元边界才响应），这里在每个单元和每批写入前再确认一次，把
    "资料已删、任务还在写" 的窗口压到最小。

    参数：
        doc_id: 当前导入/重建的资料 id。
    异常：
        JobCancelled: 资料行已经不存在（被删除）。
    """
    if store.get(int(doc_id)) is None:
        raise JobCancelled("资料已被删除，停止写入")


@dataclass(frozen=True)
class ImportResult:
    """一份文件的导入结果。"""

    status: str
    """``imported`` / ``skipped`` / ``failed``。"""

    source_name: str
    reason: str = ""
    """给人看的原因（跳过或失败时）。"""

    doc_id: int | None = None
    chunks: int = 0
    notes: list[str] = field(default_factory=list)
    #: 失败分类（"bad_request" / "unsupported" / "not_found"…）；成功时为空串。
    #: 让**任务层不用去猜原因文本**（以前靠 `"不支持" in reason`，太脆）。
    error_kind: str = ""
    """成功但有要提醒的事（比如"PDF 没有文字层，可能是扫描件"）。"""

    diagnostics: tuple[Diagnostic, ...] = ()
    """切分过程中的降级/异常（硬切、跳过图片、单行超预算等），写进任务事件，界面可见。"""


def _fingerprint(text: str) -> str:
    """正文指纹：去掉所有空白后的 sha256（为空表示正文里没有有效内容）。"""
    stripped = re.sub(r"\s+", "", text)
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest() if stripped else ""


class _NothingToIndex(Exception):
    """没有可检索的文字（扫描件没 OCR、空文档等）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason or "没有可检索的文字")
        self.reason = reason


class _ImportRun:
    """流式导入的**累计值**（正文哈希、摘要、计数都在流的过程中长出来）。"""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.notes: list[str] = []
        self.ocr_pages: list[int] = []
        self.groups = 0
        self.rows = 0
        self.seq = 0
        self.pages: int | None = None
        #: 文档大标题（第一个 1 级标题）——单独存 documents.doc_title，不进 section_path
        self.doc_title = ""
        #: 切分诊断（降级/异常）收集器；结束时随 ImportResult 交给任务层。
        self.sink = DiagnosticSink()

    @property
    def text(self) -> str:
        return "\n".join(self.text_parts)


def _write_batch(
    doc_id: int,
    groups: Sequence[object],
    *,
    start_seq: int,
    version: int,
    status: str,
    embed: object | None = None,
    doc_title: str = "",
) -> tuple[int, int]:
    """写一批片段到 **Qdrant**（child dense + parent 无向量集合），返回 ``(子块数, 下一 seq)``。

    **这一批就是"嵌入 + 写"两步**：child 文本批量嵌入成 dense 向量后写向量集合；
    parent 只存文本（进无向量集合，检索命中 child 后按 ``parent_key`` 取它）。
    不再写 SQLite chunks/codes。``embed`` 缺省用真实模型（测试注入）。

    返回的 ``seq`` 是**下一个可用子块序号**：parent 不占子块序号，所以子块编号连续
    （0..n-1），下一批从这里接着编号。
    """
    return store.write_tree(
        doc_id, groups, start_seq=start_seq, version=version,
        status=status, embed=embed, doc_title=doc_title,
    )


def _stream_into(
    path: Path,
    source_name: str,
    doc_id: int,
    outcome: _ImportRun,
    *,
    version: int = 1,
    chunk_status: str = "active",
    on_progress: ProgressFn | None = None,
    embed: object | None = None,
) -> None:
    """把一份文件**流式**跑完四个阶段：解析 → 结构识别 → 切分 → 写入（含嵌入）。

    单遍流式的含义：**喂一个"结构单元"进来，就切完、嵌完、写完、丢掉**，再进下一个单元——
    内存里只有"当前单元 + 当前一个 parent"，不是整份文档的所有片段。四个阶段：

    1. **解析**（``iter_units``）：按文件格式把字节变成有序 ``Unit``（PDF 一页 / docx 一个
       body 元素 / xlsx 一个表格段 / pptx 一张幻灯片 / txt 一段）。解析器只负责"这页有什么"
       （文字、表格、页码、字号/加粗、行号等定位原料），**不做任何切分**。
    2. **结构识别**（``Structurer.feed``）：给每个块补 ``section_path`` 与 ``level``
       （"这段属于哪个标题下"）。``Structurer`` **有状态**（标题栈）：一个 parent 可能跨多个
       单元，标题上下文必须跨 ``feed`` 保留；PDF 书签在首个单元带上后用它初始化。
    3. **切分**（``Splitter.feed`` / ``flush``）：把块切成"父块 + 子块"组。标题开新 parent；
       正文按句级贪心聚合成 child（受 token 预算与重叠约束）；表格/代码作为原子块。
       切分器同样**有状态**：只缓冲当前一个 parent；``feed`` 返回本单元里**已封口**的组，
       未封口的留在切分器里等后续单元，``flush`` 收尾最后一块。
    4. **写入**（``_write_batch`` → ``store.write_tree``）：每个已封口的组先给 child 做
       **嵌入**（dense 向量），再连同 payload **写入 Qdrant**（child 进向量集合、parent 进
       无向量集合）。写入状态由 ``chunk_status`` 决定：导入时是 ``pending``（检索看不到），
       等揭示事务再统一提升为 ``active``。

    ``on_progress`` 在**每个单元边界**回调（进度 + 取消/退出的检查点）；它落在无事务窗口里，
    所以进度写库可以安全地自己开事务。
    """
    # ---- 循环外先建好：下面两个对象都**有跨单元的状态**，不能每个单元重建 ----
    # 结构识别器：内部维护"标题栈"（当前处在哪个章节）。一个 parent 可能横跨多个单元，
    # 所以标题上下文必须跨 feed 保留。
    # **每个文档都必须有一个 H1**（标题路径从 H1 起）：
    # - 原生第一块就是 H1（docx/md 标题、PPT 页标题、Excel sheet 名）→ 直接用它；
    # - 否则（csv/json/log/txt/html，或 PDF/MD 开头是正文）→ 用**文件名**造一个 H1。
    # 造出来的 H1 就是"文档大标题"（从 section_path 摘掉、由 payload 统一拼在最前面），
    # 所以除 PPT/Excel 外都按 doc_title 处理；PPT/Excel 的 H1 是页标题/sheet 名，保留在路径里。
    suffix = Path(source_name).suffix.lower()
    unit_iter = iter(iter_units(path, name=source_name))
    first_unit = next(unit_iter, None)
    native_h1 = bool(
        first_unit
        and first_unit.blocks
        and first_unit.blocks[0].content_type == TYPE_HEADING
        and int(first_unit.blocks[0].level or 0) == 1
    )
    # 造出来的 H1 就是"文档大标题"：即使 PPT/Excel，合成的文件名标题也不当页/sheet 章节；
    # 只有"原生 H1 + PPT/Excel"才把标题保留在路径里（页标题/sheet 名）。
    doc_title_from_h1 = (not native_h1) or suffix not in (".pptx", ".xlsx", ".xlsm")
    if first_unit is not None and not native_h1:
        title = Path(source_name).stem.strip() or source_name
        first_unit.blocks.insert(0, Block(text=title, content_type=TYPE_HEADING, level=1))
    structure = Structurer(
        toc=first_unit.toc if first_unit is not None else None,
        doc_title_from_h1=doc_title_from_h1,
    )
    # 切分器：只缓冲"当前一个未封口的 parent"（外加一个挂起的标题），同样要跨 feed 保留；
    # from_config() 按**当前设置**读 parent/child 尺寸等参数（界面改过立刻生效）。
    engine = Splitter.from_config()

    # ---- 阶段 1：解析——逐单元产出（一次一个单元在内存，大文件不整包读）----
    # iter_units 按后缀分派到 pdf/docx/xlsx/pptx/text 解析器，并统一清洗不可见字符。
    # 第一个单元已经预读（上面判 H1 用），这里拼回流里继续。
    unit_stream = chain([first_unit], unit_iter) if first_unit is not None else iter(())
    #: 写入攒批：把一个单元切出来的组**攒到 ≥ WRITE_BATCH_CHILDREN 个子块再统一嵌入+写**。
    #: 为什么：逐组（一个 parent 一组，约 5~10 个子块）调用嵌入与 Qdrant 写入，批太小、
    #: 单次 forward 的固定开销占大头，实测吞吐只有大批的 1/2~1/4。攒批后仍受"只压一个
    #: parent"之外的一批组（≤ 256 个子块 ≈ 100KB 文本）约束，内存可控。
    #: ⚠ 这会改变**嵌入批的行组成**，而这张 int8 模型的输出对批内行数敏感（见 tokenization
    #: 模块说明）——升级后已有语料需全量重建才能与新批口径一致（当前库很小，代价可忽略）。
    pending_groups: list[object] = []
    pending_children = 0
    units = 0
    #: 底层/测试可以直接调用本函数、没有 documents 行；只有**开始时存在**才启用
    #: "资料被删就停"的检测（真实导入/重建在开始前一定已经建好行）。
    track_document = store.get(int(doc_id)) is not None

    def _check_document() -> None:
        if track_document:
            _ensure_document_alive(doc_id)

    for units, unit in enumerate(unit_stream, start=1):
        # 删除资料后立刻停止：不要等下一个取消检查点再写一批孤儿点
        _check_document()
        if unit.pages is not None:
            outcome.pages = unit.pages
        outcome.notes.extend(unit.notes)
        outcome.ocr_pages.extend(
            block.page for block in unit.blocks if block.content_type == TYPE_IMAGE and block.page
        )
        if unit.toc:
            # PDF 书签是**文档级**信息（iter_units 只在首个单元带一次）；用它重建结构识别器
            # ——书签是作者给的原生结构，比字号/加粗的启发式准。
            structure = Structurer(toc=unit.toc, doc_title_from_h1=doc_title_from_h1)

        # ---- 阶段 2：结构识别——给块补 section_path / level ----
        # 标题栈在这里推进：标题块拿到自己的层级，正文块拿到"所属章节的深度"。
        blocks = structure.feed(unit.blocks)
        for block in blocks:
            if block.content_type != TYPE_IMAGE:
                # 图片占位块没有文字，不进正文指纹（等 OCR 出文字后才有内容）。
                outcome.text_parts.append(block.text)

        # ---- 阶段 3：切分——切出本单元里已封口的 parent+child 组 ----
        # 未封口的 parent 留在切分器里，等后续单元接着喂（这正是它要有状态的原因）。
        # 先同步文档大标题：标题路径从 H1 起，前缀预算要把它算进去。
        if on_progress is not None:
            # 进度用"这个单元里最大到第几页"：PDF/PPT 有页码，txt/docx/xlsx 没有 → 退回单元序号。
            # 总页数只有 PDF/PPT 报得准；不知道就报 total=0（界面显示不确定进度）。
            page_now = max(
                (block.page_to or block.page or 0 for block in unit.blocks), default=0
            )
            if outcome.pages:
                done = page_now or units
                note = f"解析到第 {done} 页"
            else:
                done = units
                note = f"解析第 {units} 个单元"
            on_progress(done, outcome.pages or 0, note)

        engine.set_doc_title(structure.doc_title)
        groups = engine.feed(blocks)
        if groups:
            pending_groups.extend(groups)
            pending_children += sum(len(group.children) for group in groups)  # type: ignore[attr-defined]
            # **按阈值分批写**：一个单元可能一次吐出几百个组（典型：xlsx 整张表）。
            # 旧写法在"整块加完"之后才判断阈值，结果第一批就是全量，Qdrant 在嵌入完
            # 全部片段前一直是 0 条、界面完全不动。这里只取够一批的组先写。
            while pending_children >= WRITE_BATCH_CHILDREN:
                batch: list[object] = []
                batch_children = 0
                while pending_groups:
                    group = pending_groups[0]
                    size = len(getattr(group, "children", ()))
                    if batch and batch_children + size > WRITE_BATCH_CHILDREN:
                        break
                    pending_groups.pop(0)
                    batch.append(group)
                    batch_children += size
                    if batch_children >= WRITE_BATCH_CHILDREN:
                        break
                if not batch:
                    break
                _check_document()
                children, outcome.seq = _write_batch(
                    doc_id, batch, start_seq=outcome.seq, version=version,
                    status=chunk_status, embed=embed,
                    doc_title=structure.doc_title or Path(source_name).stem,
                )
                outcome.rows += children
                outcome.groups += len(batch)
                pending_children -= batch_children
                if on_progress is not None:
                    # 已经攒下的总量（已完成 + 还没写）对表格类文件是准确的；先给个
                    # 可判读的分母，界面进度条才能从"一直不动"变成百分比。
                    suffix = Path(source_name).suffix.lower()
                    table_like = suffix in {".xlsx", ".xlsm", ".csv", ".tsv"}
                    known_total = outcome.rows + pending_children if table_like else 0
                    note = (
                        f"向量化 {outcome.rows}/{known_total} 个片段…"
                        if known_total
                        else f"向量化 {outcome.rows} 个片段…"
                    )
                    on_progress(outcome.rows, known_total, note)

    # 收尾：文件读完，把切分器里最后还开着的 parent 封口，和攒着的组一起写出去。
    # 没有原生大标题（csv/json/log/txt/html 等）→ 用文件名兜底，界面/引用也有"文档标题"
    outcome.doc_title = structure.doc_title or Path(source_name).stem
    pending_groups.extend(engine.flush())
    # 把切分过程中攒下的诊断（硬切/跳过图片/单行超预算）并进本次导入结果
    outcome.sink.extend(engine.sink)
    if pending_groups:
        _check_document()
        children, outcome.seq = _write_batch(
            doc_id, pending_groups, start_seq=outcome.seq, version=version,
            status=chunk_status, embed=embed,
            doc_title=outcome.doc_title or Path(source_name).stem,
        )
        outcome.rows += children
        outcome.groups += len(pending_groups)
        if on_progress is not None:
            on_progress(
                outcome.rows,
                outcome.rows,
                f"向量化 {outcome.rows} 个片段…",
            )
    outcome.ocr_pages = sorted({page for page in outcome.ocr_pages if page})
