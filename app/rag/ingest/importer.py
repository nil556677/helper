"""导入路径：文件字节 → 资料行 + 片段行（含索引文本）。

负责认领/判重/写副本/失败收尾等导入侧逻辑；重建相关放在 ``rebuilder``。
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.db.connection import close_thread_connection, transaction
from app.core.embed import EmbedError
from app.core.jobs.model import JobCancelled, JobInterrupted
from app.core.parse import ParseError
from app.core.paths import file_size
from app.core.versioning import CHUNK_POLICY_VERSION, EMBEDDING_VERSION, PARSER_VERSION
from app.rag import store

from .progress import ProgressFn
from .rebuilder import _discard_copy
from .streaming import (
    ImportResult,
    _fingerprint,
    _ImportRun,
    _NothingToIndex,
    _stream_into,
)


def _safe_name(name: str) -> str:
    """把文件名净化成可落盘的形状（保留中文；去路径/非法字符/保留名；超长截断保后缀）。

    实现收口在 :func:`app.core.paths.safe_filename`——上传暂存与副本落盘用同一套规则，
    避免"暂存区能落、副本区落不了"（保留名/超长名）这种两处规则漂移。
    """
    return paths.safe_filename(name)


def _existing_by(column: str, value: str, *, exclude: int | None = None) -> int | None:
    """判重查询（SQL 在 store 里）；``exclude`` 排除自己那一行。"""
    return store.find_id_by(column, value, exclude_id=exclude)


def _record_failure(name: str, size: int, reason: str) -> int:
    """把失败也记进资料表（用户在列表里能看到它、也能删掉它）。"""
    return store.record_failure(name, size, reason)


def import_path(
    path: Path,
    filename: str,
    *,
    on_progress: ProgressFn | None = None,
    claim_doc_id: int | None = None,
    embed: object | None = None,
) -> ImportResult:
    """从**磁盘路径**导入一份文件（大文件的入口：不整包读进内存）。

    **流式管线**（P1.3 起）：``iter_units``（一页/一段/一元素）→ ``structure.feed``
    （标题栈跨单元保留）→ ``Splitter.feed``（只压一个 parent）→ **逐 parent 组边流边写**。
    内存上限是"一个 parent"，不是整份文档的片段列表。

    **从"锁上做原子"改成"状态上做原子"**（本轮重构，替代旧版"一个跨整段解析的
    ``BEGIN IMMEDIATE``"）：每一组片段在**自己的短事务**里写，全部片段先统一 `pending`；
    解析完且判重通过后，一个短事务**揭示**——补全文档摘要、并把首个版本的片段提升为
    ``active``（检索只认 ``active``，半截不会被搜到）。**失败必删**：重复/取消/失败都显式
    清掉候选行及其 pending 片段（外键级联），不再靠整段回滚。批与批之间不持锁，
    ``on_progress`` 落在无事务窗口里，进度写入可安全地自己开事务。

    正文判重（``text_hash``）在流式结束后算：算完发现库里有同样内容就**作废本次候选**
    （删行 + 删刚落的副本），一份也不留——不必把整份文档解析两遍。

    **版本链**（P1.7）：同一 ``doc_key``（规范化文件名）已经有资料时，这次导入是**新版本**——
    版本号 +1、状态 ``indexing``、片段初始 ``pending``（等向量化完成后再原子切换）；
    首个版本在揭示事务里把片段提升成 ``active``（词法立刻可搜）。

    参数：
        path: 源文件路径（inbox 的暂存文件 / 命令行给的文件）。
        filename: 原始文件名（判重、副本命名与展示都用它）。
        on_progress: 每处理完一个结构单元回调 ``(done, total, note)``（进度/取消检查点）。
    返回：:class:`ImportResult`。
    """
    source_name = Path(filename).name

    # ① 预处理：读文件（字节哈希）→ 一级判重 → 落副本 → 校验认领行。
    #    任何一步不通过都直接返回 ImportResult，不再往下走。
    staged = _prepare(path, source_name, claim_doc_id=claim_doc_id)
    if isinstance(staged, ImportResult):
        return staged

    # ② 落库 + 解析/切分/嵌入/写 + 正文判重 + 揭示（阶段细节见 _ingest 与 _stream_into）。
    return _ingest(staged, on_progress=on_progress, embed=embed)


@dataclass
class _Staged:
    """预处理阶段的产物：源文件已判重、副本已落盘、认领行已校验。

    后面几步只依赖这些字段，不再回头看 ``path``/``filename`` 原始参数。
    """

    source_name: str
    """原始文件名（展示 / 判重 / 副本命名用）。"""

    source: Path
    """源文件路径（读正文用；inbox 暂存文件或命令行给的文件）。"""

    stored: Path
    """``data/files/<hash12>/<安全名>`` 下的副本（失败收尾时删它）。"""

    stored_path: str
    """相对路径（写进 ``documents.stored_path``；形如 ``<hash12>/<名>``）。"""

    file_hash: str
    """文件字节 sha256（一级判重键，也是副本目录名）。"""

    size: int
    """文件字节数（写库用，避免反复 stat）。"""

    claim_doc_id: int | None
    """入队时占位行的 id；None 表示"没有认领行，由本次新建"。"""


@dataclass
class _Attached:
    """第三段的产物：资料行已建/已更新，版本链已确定。"""

    doc_id: int
    """资料 id（认领行沿用占位行 id，否则是本次新建的）。"""

    doc_key: str
    """规范化文件名（版本链的键）。"""

    version: int
    """本次的版本号（同一 doc_key 首次为 1，之后 +1）。"""

    previous_ready: object
    """上一个就绪版本的行（没有则 None）；揭示事务用它做原子切换。"""


def _prepare(
    path: Path, source_name: str, *, claim_doc_id: int | None
) -> _Staged | ImportResult:
    """第一段：算出字节哈希 → 一级判重 → 落副本 → 校验认领行。

    返回 ``_Staged``（继续导入）或 ``ImportResult``（就地收尾：跳过/失败）。
    """
    try:
        file_hash = _stream_sha256(path)
    except OSError as exc:
        return ImportResult("failed", source_name, reason=f"读不到文件：{exc}", error_kind="not_found")
    if not file_hash:
        return ImportResult("failed", source_name, reason="文件是空的", error_kind="bad_request")

    stopped = _skip_if_already_imported(source_name, file_hash, claim_doc_id)
    if stopped is not None:
        return stopped

    size = file_size(path)
    copied = _copy_to_files(path, source_name, file_hash, size)
    if isinstance(copied, ImportResult):
        return copied
    stored = copied

    stopped = _check_claimable(claim_doc_id, source_name, stored)
    if stopped is not None:
        return stopped

    return _Staged(
        source_name=source_name,
        source=path,
        stored=stored,
        stored_path=f"{file_hash[:12]}/{stored.name}",
        file_hash=file_hash,
        size=size,
        claim_doc_id=claim_doc_id,
    )


def _skip_if_already_imported(
    source_name: str, file_hash: str, claim_doc_id: int | None
) -> ImportResult | None:
    """一级判重：按**文件字节哈希**看库里是否已有同样内容。

    返回 None 表示"继续导入"；返回 ImportResult 表示"就此收尾（跳过）"。
    失败占位行不挡重试：先清掉它，再当全新导入。
    """
    # **排除认领的那一行**：占位行在入队时就带着 file_hash（P1.16），不排除的话
    # 导入会把自己判成"重复"，然后什么都不做、占位行永远停在 pending（实测踩过）。
    known = _existing_by("file_hash", file_hash, exclude=claim_doc_id)
    if known is None:
        return None
    known_row = store.get(int(known))
    if known_row is not None and str(known_row["status"]) == "failed":
        # 失败占位行不挡重试：清掉（含可能残留的点）再当全新导入
        logger.info("上次导入失败（doc {}），本次重试", known)
        _wipe_candidate(int(known))
        return None
    if (
        known_row is not None
        and str(known_row["status"]) == "pending"
        and not str(dict(known_row).get("stored_path") or "")
    ):
        # **从未开始的占位行**（stored_path 为空）：没有副本、也没内容，不能当"已经在库里"
        # 挡住重导——否则用户取消一次导入后，同文件永远传不进来。清掉占位再继续。
        logger.info("上次导入未开始（doc {} 无副本），本次重新导入", known)
        _wipe_candidate(int(known))
        return None
    _wipe_candidate(claim_doc_id)  # 真有重复：占位行没有意义了（此时还没落副本）
    return ImportResult("skipped", source_name, reason="这份文件已经在资料库里了", doc_id=known)


def _copy_to_files(path: Path, source_name: str, file_hash: str, size: int) -> Path | ImportResult:
    """把源文件复制到内容寻址的副本目录（``data/files/<hash12>/``）。

    ``shutil.copyfile`` 是**分块复制**（每块 64 KiB），内存占用恒定——不把整份读进内存，
    这也是"大文件"路径能成立的前提之一。副本按内容哈希分目录，同一内容天然共享位置。
    """
    folder = paths.files_dir() / file_hash[:12]
    stored = folder / _safe_name(source_name)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, stored)  # 分块复制（每块 64 KiB），不整包读进内存
    except OSError as exc:
        doc_id = _record_failure(source_name, size, f"写副本失败：{exc}")
        logger.error("写资料副本失败：{}", exc)
        return ImportResult(
            "failed", source_name, reason=f"写副本失败：{exc}", doc_id=doc_id, error_kind="io"
        )
    return stored


def _check_claimable(
    claim_doc_id: int | None, source_name: str, stored: Path
) -> ImportResult | None:
    """校验认领行还在且状态可写。

    认领前先确认那一行还在：用户可能已经把"待处理"的那一行删掉了。不检查的话会往一个
    不存在的 doc_id 上写片段（要么撞外键、要么留下孤儿）。
    """
    if claim_doc_id is None:
        return None
    claimed = store.get(int(claim_doc_id))
    if claimed is None:
        _discard_copy(stored)
        return ImportResult(
            "failed",
            source_name,
            reason="排队时的那一行已经不在了（可能已被删除）",
            error_kind="not_found",
        )
    if str(claimed["status"]) not in ("pending", "failed", "indexing"):
        # **重复投递**（P1 审查 H1）：这一行已经在库里（ready）或已退场（superseded）。
        # indexing = 上次崩溃半写残留：Qdrant 已有 pending 点，**允许重入**，
        # 由 write_tree 按相同 key 幂等覆盖（不跳过，否则行永远卡 indexing）。
        _discard_copy(stored)
        return ImportResult(
            "skipped",
            source_name,
            reason="这份资料已经在库里了（重复投递）",
            doc_id=int(claim_doc_id),
        )
    return None


def _ingest(staged: _Staged, *, on_progress: ProgressFn | None, embed: object | None) -> ImportResult:
    """第二段：建/更新资料行 → 解析/切分/嵌入/写 → 正文判重 → 揭示。

    这里的 ``try/except`` 是导入的**状态机核心**：每一类异常对应一种收尾动作
    （作废候选、判失败、放回队列）。所有"失败"分支都显式删候选行（外键级联清片段），
    而不是靠整段事务回滚——因为片段是边解析边落库的，没有"整段事务"可回滚。
    """
    now = time.time()
    if isinstance(attached := _attach_document(staged, now=now), ImportResult):
        return attached
    # doc_id 在下面的失败分支里会被 _fail() 换成新 id，所以单独取一份可变量；
    # 其余字段只读，直接用 attached.<字段>，不必再解包。
    doc_id = attached.doc_id

    outcome = _ImportRun()
    try:
        # 解析 + 切分 + 嵌入 + 写都在这里（单遍流式，细节见 _stream_into）。
        # 片段先统一写 pending：检索只认 active，揭示前任何半截都不会被搜到。
        _stream_into(
            staged.source,
            staged.source_name,
            doc_id,
            outcome,
            version=attached.version,
            chunk_status="pending",
            on_progress=on_progress,
            embed=embed,
        )
        if outcome.groups == 0 or not outcome.text.strip():
            raise _NothingToIndex(outcome.notes[0] if outcome.notes else "")

        # 二级判重：按**正文指纹**（去空白后的 sha256）看库里是否已有同样内容。
        # 放在流式结束后算——不必把整份文档解析两遍；命中就作废本次候选。
        text_hash = _fingerprint(outcome.text)
        duplicate = _existing_by("text_hash", text_hash, exclude=doc_id)
        if duplicate is not None:
            raise _DuplicateContent(duplicate, staged.file_hash)

        _publish(
            doc_id,
            attached.doc_key,
            attached.version,
            attached.previous_ready,
            outcome,
            text_hash,
        )
    except _DuplicateContent as dup:
        logger.info("同样的内容已在库里（doc {}），本次导入作废", dup.doc_id)
        _discard_copy(staged.stored)
        # **不刷新 file_hash**（P7 审查修正）：二级判重命中的是"正文相同、文件字节不同"的
        # 另一份文件；把本次的字节哈希写到**它的行**上，副本却还是旧文件的——重建时按副本
        # 重算哈希会翻转，判重口径与副本自相矛盾。宁可下次慢一点（走到正文判重再 skip）。
        _wipe_candidate(doc_id)  # 失败必删：候选行及其 pending 片段一起清掉
        return ImportResult(
            "skipped", staged.source_name, reason="同样的内容已经在资料库里了", doc_id=dup.doc_id
        )
    except _NothingToIndex as nothing:
        logger.info("没有可检索的文字：{}（{}）", staged.source_name, nothing.reason)
        _discard_copy(staged.stored)
        reason = "没有可检索的文字"
        if nothing.reason:
            reason = f"{reason}：{nothing.reason}"
        doc_id = _fail(doc_id, staged.source_name, staged.source, reason)
        return ImportResult(
            "failed", staged.source_name, reason=reason, doc_id=doc_id, error_kind="bad_request"
        )
    except ParseError as exc:
        logger.info("导入失败：{}（{}）", staged.source_name, exc)
        _discard_copy(staged.stored)
        doc_id = _fail(doc_id, staged.source_name, staged.source, str(exc))
        return ImportResult(
            "failed", staged.source_name, reason=str(exc), doc_id=doc_id, error_kind=_parse_kind(exc)
        )
    except EmbedError as exc:
        # **模型问题重试也不会变好**（模型缺失 / SHA 不符 / onnxruntime 起不来）：
        # 单独给 "model" 类，任务层据此判不可重试——之前落进 "unknown" 会白跑三次完整解析。
        logger.error("嵌入模型不可用（{}）：{}", staged.source_name, exc)
        _discard_copy(staged.stored)
        doc_id = _fail(doc_id, staged.source_name, staged.source, f"嵌入失败：{exc}")
        return ImportResult(
            "failed",
            staged.source_name,
            reason=f"嵌入失败：{exc}",
            doc_id=doc_id,
            error_kind="model",
        )
    except (JobCancelled, JobInterrupted) as exc:
        # **取消/退出不是失败**：进度回调会在单元边界抛这两个异常（用户点了取消，或应用要退出）。
        # 走下面的宽 except 会被包成"入库失败"，用户看到的就是"取消失败了"。
        _discard_copy(staged.stored)
        if isinstance(exc, JobCancelled):
            # 用户不要了：候选行（含占位）及其 pending 片段一起删掉
            _wipe_candidate(doc_id)
        else:
            # **退出中断**：任务会被放回队列，下次要认领同一行。这里**不能删行**——
            # 删了之后重跑的 payload.doc_id 指向不存在的行，claim 检查直接判"那一行已经不在了"，
            # "关窗口接着做"就变成了"关窗口必失败"。退回 pending，等下次重跑（重跑会先清旧片段）。
            if doc_id is not None:
                try:
                    store.set_status(None, int(doc_id), "pending", detail="上次被中断，等待重跑")
                except Exception as exc2:  # 状态没退回去也不该挡住中断传播
                    logger.warning("中断收尾时复位占位行失败（doc {}）：{}", doc_id, exc2)
        raise
    except Exception as exc:
        logger.exception("资料入库失败（{}）：{}", staged.source_name, exc)
        _discard_copy(staged.stored)
        doc_id = _fail(doc_id, staged.source_name, staged.source, f"入库失败：{exc}")
        return ImportResult(
            "failed", staged.source_name, reason=f"入库失败：{exc}", doc_id=doc_id, error_kind="unknown"
        )

    logger.info(
        "已入库：{}（{} 个父块 / {} 个片段）", staged.source_name, outcome.groups, outcome.rows
    )
    return ImportResult(
        "imported",
        staged.source_name,
        doc_id=doc_id,
        chunks=outcome.rows,
        notes=list(outcome.notes),
        diagnostics=tuple(outcome.sink.items()),
    )


def _attach_document(staged: _Staged, *, now: float) -> _Attached | ImportResult:
    """第三段：版本链 + 建/更新资料行（一个短事务，只占写锁一瞬）。

    **版本链**（P1.7）：同一 ``doc_key``（规范化文件名）已经有资料时，这次导入是**新版本**——
    版本号 +1；``previous_ready`` 记下"上一个就绪版本"，供揭示时原子切换。
    版本号与上一版在**同一个事务**里算（``BEGIN IMMEDIATE`` 拿写锁，读最大版本→插入不被人插队），
    但**不跨整段解析持锁**。

    **认领路径的版本口径**（P7 审查修正）：占位行在 ``insert_pending`` 时就已在事务内
    分配了 version——认领时**沿用占位行的 version**，而不是重算 ``next_version``。
    重算会把"占位行自己"也算进 MAX，得到 version+1：向量 payload 用 v+1、DB 行还是 v，
    切换/收敛按 DB version 找 key 就找不到点（崩溃重跑必现）。

    **崩溃重跑的残留清理也改成按版本**：只清**候选版本自己**（=占位行 version）的半截点；
    绝不 `delete_chunks(doc_id)` 全删——那会把**在服版本**的向量也删掉，导入失败=
    在服资料静默失搜。Qdrant 调用放在 SQLite 事务**外**（它不可回滚）。
    """
    doc_key = store.doc_key_for(staged.source_name)
    doc_id = int(staged.claim_doc_id) if staged.claim_doc_id is not None else None
    claimed_version: int | None = None
    if doc_id is not None:
        claimed_row = store.get(doc_id)
        if claimed_row is not None:
            claimed_version = int(claimed_row["version"] or 1)
        if claimed_version is not None:
            from .rebuilder import _drop_version_points

            _drop_version_points(doc_id, claimed_version)
    version = 1
    previous_ready = None
    try:
        with transaction() as conn:
            current = store.current_row_by_key(doc_key)
            if doc_id is not None and claimed_version is not None:
                version = claimed_version
            else:
                version = store.next_version(doc_key) if current is not None else 1
            previous_ready = (
                store.latest_ready_row(doc_key, before_version=version)
                if current is not None
                else None
            )
            if staged.claim_doc_id is not None:
                # **认领入队时落的占位行**（P1.16）：同一个 id 继续写片段，
                # 列表从"待处理"直接翻成"处理中"，用户看到的始终是同一份资料。
                store.set_status(
                    conn, doc_id, "indexing", detail="解析中", text_hash=None, now=now
                )
                conn.execute(
                    "UPDATE documents SET stored_path = ?, file_hash = ?, size = ? WHERE id = ?",
                    (staged.stored_path, staged.file_hash, staged.size, doc_id),
                )
                # 旧 SQLite 片段（老库升级残留）若存在也清掉；Qdrant 侧已在上面按版本清过。
            else:
                doc_id = store.insert_ready(
                    conn,
                    source_name=staged.source_name,
                    stored_path=staged.stored_path,
                    file_hash=staged.file_hash,
                    # 真正的正文哈希要等流式读完全文才知道；先留 **NULL**（不是文件哈希！）
                    text_hash=None,
                    size=staged.size,
                    detail=None,
                    doc_key=doc_key,
                    version=version,
                    # 词法已可用、向量还没建 → indexing（技术方案 §9.7 的状态机）
                    status="indexing",
                    now=now,
                )
    except Exception as exc:
        logger.exception("资料创建失败（{}）：{}", staged.source_name, exc)
        _discard_copy(staged.stored)
        doc_id = _fail(doc_id, staged.source_name, staged.source, f"入库失败：{exc}")
        return ImportResult(
            "failed", staged.source_name, reason=f"入库失败：{exc}", doc_id=doc_id, error_kind="unknown"
        )
    return _Attached(
        doc_id=doc_id, doc_key=doc_key, version=version, previous_ready=previous_ready
    )


def _verify_written(doc_id: int, version: int, outcome: _ImportRun) -> None:
    """写入后校验：Qdrant 里的点数必须与流式写入报告一致，否则**不揭示**。

    为什么信任这一层：流式写入是分批的，中途"报告成功但没写进去"的情况（批丢失、
    Qdrant 报错被吞）在单测里很难覆盖；用"点数对账"兜底，宁可这份资料失败可重试，
    也不要揭示一份缺块的索引。
    """
    from app.core.vector import api as vector_api
    from app.core.vector import qdrant_store

    def _count(collection: str) -> int:
        return len(
            qdrant_store.scroll_filter(
                collection,
                {"documents_id": int(doc_id), "version": int(version)},
                with_vectors=False,
            )
        )

    children = _count(vector_api.COLLECTION_CHUNKS)
    parents = _count(vector_api.COLLECTION_PARENTS)
    # 父块允许**按内容去重**（多个子块引用同一父块），所以父块数 ≤ 组数即可；
    # 子块必须严格对得上，少了就是写入丢数据。
    if children != outcome.rows or parents > outcome.groups:
        raise RuntimeError(
            f"写入校验失败：期望 {outcome.rows} 子块 / ≤{outcome.groups} 父块，"
            f"实际 {children} / {parents}"
        )


def _publish(
    doc_id: int,
    doc_key: str,
    version: int,
    previous_ready: object,
    outcome: _ImportRun,
    text_hash: str,
) -> None:
    """第四段：揭示（一个短事务）—— 补全文档摘要，并把候选片段提升为 ``active``。

    **状态做"成功确认"**：揭示前所有片段是 ``pending``（检索看不到），揭示后才 ``active``。
    新版本有上一版在服务时保持 ``pending``，由 ``switch_version`` 原子切换；首个版本直接
    ``mark_ready``（词法立刻可搜）。
    """
    # 先校验（放事务外：Qdrant scroll 可能慢，不该占着 SQLite 写锁）
    # 资料可能在流式阶段被删除（用户先删资料、后取消任务）：此时绝不能"揭示"——
    # finish_document 会 UPDATE 0 行、promote 却把孤儿点标成 active。
    if store.get(int(doc_id)) is None:
        raise JobCancelled("资料已被删除，放弃揭示")
    _verify_written(doc_id, version, outcome)
    with transaction() as conn:
        updated = store.finish_document(
            conn,
            doc_id,
            text_hash=text_hash,
            pages=outcome.pages,
            ocr_pages=outcome.ocr_pages,
            chars=len(outcome.text),
            detail=outcome.notes[0] if outcome.notes else None,
            doc_title=outcome.doc_title or None,
        )
        if not updated:
            # 资料行在流式写入/校验期间被删除：事务回滚，外层按取消清理 Qdrant 点。
            raise JobCancelled("资料已被删除，放弃揭示")
        store.set_pipeline_versions(
            conn,
            doc_id,
            parser_version=PARSER_VERSION,
            chunk_policy=CHUNK_POLICY_VERSION,
            embedding_version=EMBEDDING_VERSION,
            verified_at=time.time(),
        )
        store.promote_chunks(conn, doc_id)
        if previous_ready is not None:
            store.switch_version(conn, int(previous_ready["id"]), doc_id)
            # 收敛被更高版本超车的候选（与 embedding 旧路径口径一致，P7 审查）
            store.supersede_stale_candidates(conn, doc_key, before_version=version)
        else:
            store.mark_ready(conn, doc_id, detail=None)
    # 统计缓存（覆盖率/占用/进度）立即失效：下一次总览读到的是新值，不用等 TTL
    try:
        from app.rag.store import stats as store_stats

        store_stats.clear_caches()
    except Exception:
        pass


class _DuplicateContent(Exception):
    """流式读完发现正文与库里某份一致（**故意抛异常让事务回滚**）。"""

    def __init__(self, doc_id: int, file_hash: str) -> None:
        super().__init__(f"正文与资料 {doc_id} 重复")
        self.doc_id = doc_id
        self.file_hash = file_hash


def _wipe_candidate(doc_id: int | None) -> None:
    """删掉一份**还没揭示**的候选资料（重复/取消时用：这份资料什么都没留下）。

    旧实现靠"一个跨整段解析的事务"把没成的东西整体回滚；改成边解析边落库后，失败/取消
    必须**显式删除**已经写下去的 pending 片段——``documents → chunks → codes`` 都是
    ``ON DELETE CASCADE``，删这一行即连片段、精确字段、索引一起清干净。
    """
    if doc_id is None:
        return
    try:
        store.delete_chunks(int(doc_id))
        store.delete_many([int(doc_id)])
    except Exception as exc:
        logger.warning("清理候选资料失败（doc {}）：{}", doc_id, exc)


def _fail(doc_id: int | None, source_name: str, path: Path, reason: str) -> int:
    """失败收尾：清掉未揭示的片段，把这一行标成 ``failed``（用户能看到、能删、能重试）。

    旧实现靠整段回滚扔掉片段；现在显式 ``delete_chunks``（``codes``/FTS 由外键级联 + 触发器清），
    再把 ``stored_path`` 置空（副本已扔，别留悬空路径）。行都没建出来（``doc_id is None``，
    如非认领路径的插入本身就失败）就记一条失败行。
    """
    if doc_id is None:
        return _record_failure(source_name, file_size(path), reason)
    try:
        with transaction() as conn:
            store.delete_chunks(doc_id)
            conn.execute("UPDATE documents SET stored_path = '' WHERE id = ?", (doc_id,))
        store.mark_failed(doc_id, reason)
    except Exception as exc:
        logger.warning("标记失败失败（doc {}）：{}", doc_id, exc)
        try:
            store.delete_many([int(doc_id)])
        except Exception:
            logger.exception("连失败的资料行都删不掉（doc {}）", doc_id)
    return int(doc_id)


def _parse_kind(exc: BaseException) -> str:
    """解析失败的分类（``ParseError`` 的消息里带"不支持"就是格式问题）。"""
    return "unsupported" if "不支持" in str(exc) else "bad_request"


def import_bytes(data: bytes, filename: str, *, embed: object | None = None) -> ImportResult:
    """从**字节**导入（测试/小文件；临时落盘后走 :func:`import_path`，一条代码路径）。"""
    if not data:
        return ImportResult(
            "failed", Path(filename).name, reason="文件是空的", error_kind="bad_request"
        )
    folder = paths.tmp_dir()
    folder.mkdir(parents=True, exist_ok=True)
    staged = folder / f"import-{uuid.uuid4().hex[:8]}-{Path(filename).name}"
    staged.write_bytes(data)
    try:
        return import_path(staged, filename, embed=embed)
    finally:
        staged.unlink(missing_ok=True)


def _stream_sha256(path: Path) -> str:
    """流式算文件 sha256（1 MB 块；文件为空返回空串）。"""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest() if total else ""


def import_file(path: Path) -> ImportResult:
    """导入磁盘上的一个文件（命令行/测试用；界面走上传，见 app/services/knowledge.py）。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ImportResult("failed", Path(path).name, reason=f"读不到文件：{exc}")
    return import_bytes(data, path.name)


def _cancel_jobs_for_docs(doc_ids: list[int]) -> None:
    """删除资料前，先取消所有引用这些资料的 pending/running 任务。

    为什么必须在删除里做：用户可能**先删资料、后取消任务**，也可能**先取消、后删**。
    无论哪种顺序，删除时都不该让一个还在跑的任务继续往已删除的 doc_id 写孤儿点。
    任务端在每单元/每批写入前还会用 ``_ensure_document_alive`` 兜底；这里先把
    "取消"请求发出去，让它尽快在下一个检查点停下。

    参数：
        doc_ids: 即将删除的资料 id。
    """
    from app.core.jobs import api as jobs_api

    targets = {int(doc_id) for doc_id in doc_ids}
    try:
        jobs = jobs_api.list_jobs(statuses=("pending", "running"), limit=500)
    except Exception as exc:  # 查询失败不该挡住删除（任务端还有兜底）
        logger.warning("删除前查询相关任务失败（继续删除）：{}", exc)
        return
    for job in jobs:
        payload = job.payload or {}
        if not isinstance(payload, dict):
            continue
        try:
            doc_id = int(payload.get("doc_id") or -1)
        except (TypeError, ValueError):
            continue
        if doc_id not in targets:
            continue
        try:
            jobs_api.cancel(int(job.id))
        except Exception as exc:
            logger.warning("删除资料时取消任务 #{} 失败：{}", job.id, exc)


def delete_documents(doc_ids: list[int]) -> int:
    """删掉若干份资料（片段与索引由外键级联 + 触发器一起清掉），并尽力删副本。

    **顺序与一致性**（P7 审查修正）：先删 Qdrant 点、**删成功才删 DB 行**——反过来
    （Qdrant 删一半失败还继续删行）会留下 payload 仍是 ``active`` 的孤儿点，检索照样
    召回已删内容。Qdrant 删失败的资料保持原状（用户重试删除即可），返回值只数真删掉的。

    **与任务取消的先后无关**：删除前先对引用这些 doc_id 的 pending/running 任务发取消；
    删除后任务端在每单元/每批写入前检查资料行是否还在，发现已删就停止并清理半成品。
    """
    if not doc_ids:
        return 0
    _cancel_jobs_for_docs(doc_ids)
    deleted: list[int] = []
    # 顺序一：清 Qdrant 点（chunks + parents），**失败则该资料不删 DB**。
    for doc_id in doc_ids:
        try:
            store.delete_chunks(int(doc_id))
            deleted.append(int(doc_id))
        except Exception as exc:
            logger.error(
                "删除资料 #{} 的向量点失败（保留 DB 行，可重试删除）：{}", doc_id, exc
            )
    if not deleted:
        return 0
    # 重排缓存按旧 chunk_id 记账、没有 doc_id：删资料时整体清掉，避免召回已删块的分数。
    try:
        store.clear_rerank_cache()
    except Exception as exc:  # 缓存清不掉不该挡住删除
        logger.warning("清空 rerank_cache 失败：{}", exc)
    try:
        stored_paths = store.delete_many(deleted)
    except sqlite3.OperationalError as exc:
        # 线程里的连接可能被外部弄坏（备份恢复/别的进程异常退出）：重开一次再试
        if "unable to open database file" not in str(exc):
            raise
        logger.warning("删除时数据库打不开，丢弃连接重试一次：{}", exc)
        from app.core.db.connection import log_db_diagnostics

        log_db_diagnostics("删除")
        close_thread_connection()
        stored_paths = store.delete_many(deleted)

    # **第二遍清点**：删除是"先删点、再删行"，而正在跑的任务可能在两步之间又写进一批。
    # 行已经没了，任务下一次检查会中止并清理；这里再删一次把这个竞态窗口也兜住。
    for doc_id in deleted:
        try:
            store.delete_chunks(int(doc_id))
        except Exception as exc:
            logger.warning("删除后的二次清点失败（doc {}）：{}", doc_id, exc)

    # **共用副本保护**：副本目录按内容哈希分，理论上不同行不会共享；但版本链/手工修数据
    # 都可能让两行指向同一个目录。删一份就把另一份的文件也删掉 → 之后重建会"找不到副本"。
    # 所以删之前先看"还有没有别的资料引用同一个目录"。
    in_use = {
        str(resolved.parent).lower()
        for stored in store.paths_in_use()
        if stored and (resolved := paths.resolve_stored_path(stored)) is not None
    }
    for stored in stored_paths:
        if not stored:
            continue
        resolved = paths.resolve_stored_path(stored)
        if resolved is None:
            continue  # 越界/异常路径：不碰
        folder = resolved.parent
        if str(folder.resolve()).lower() in in_use:
            logger.warning("保留副本目录（还有资料引用）：{}", folder.name)
            continue
        try:
            shutil.rmtree(folder, ignore_errors=True)  # 副本目录按哈希分，删掉就够
        except OSError as exc:
            logger.warning("删副本失败（{}）：{}", folder, exc)
    logger.info("已删除 {} 份资料", len(stored_paths))
    # 删除后统计缓存立即失效（否则总览还会显示已删资料的片段/占用）
    try:
        from app.rag.store import stats as store_stats

        store_stats.clear_caches()
    except Exception:
        pass
    return len(stored_paths)
