"""重建：用副本重新解析/切分片段、只重算词法，以及丢弃临时副本。

**重建 = 同一行升一个版本**（本轮重构，替代旧版"同版本原地先删后写"）：

- 旧做法在 SQLite 事务里先删光该资料**全部版本**的 Qdrant 点再流式重建——事务回滚时
  点不回来，**正在服务的资料静默丢向量**；且 `BEGIN IMMEDIATE` 跨整段解析（分钟级），
  期间全库写操作 5 秒超时。
- 新做法与导入的版本链同构：新树写到 **version+1、status=pending**（检索看不见），
  旧树全程原样在服；全部写完后再"揭示"——一次 `set_payloads` 把新树翻 active、
  旧树翻 superseded，最后一个短事务提交 DB（version/states）。任何一步失败都只
  需删掉新候选点（旧数据零损伤）。
"""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.db.connection import transaction
from app.core.jobs.model import JobCancelled, JobInterrupted
from app.core.parse import ParseError
from app.rag import store

from .streaming import (
    ImportResult,
    _fingerprint,
    _ImportRun,
    _NothingToIndex,
    _stream_into,
)


def _discard_copy(stored: Path) -> None:
    """删掉刚落的副本（导入没成，别在数据目录里留垃圾）。"""
    try:
        stored.unlink(missing_ok=True)
        parent = stored.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError as exc:
        logger.warning("删副本失败（{}）：{}", stored, exc)


def _drop_version_points(doc_id: int, version: int) -> None:
    """删掉某一版的全部点（child + parent）——失败/取消收尾用，只动这一版。"""
    from app.core.vector import api

    try:
        for collection, keys in (
            (api.COLLECTION_CHUNKS, store.child_keys(doc_id, version)),
            (api.COLLECTION_PARENTS, store.parent_keys(doc_id, version)),
        ):
            if keys:
                api.delete_keys(collection, keys)
    except Exception as exc:
        logger.warning("清理重建候选点失败（doc {} v{}）：{}", doc_id, version, exc)


def rebuild_document(doc_id: int, *, embed: object | None = None) -> ImportResult:
    """用**副本**重新解析并切分某份资料（换了切分参数、或解析器升级后用）。

    参数：
        doc_id: 资料 id。
        embed: 嵌入函数（测试注入；缺省真实模型）。
    返回：:class:`ImportResult`（成功时 ``chunks`` 是新的片段数）。
    """
    row = store.get(doc_id)
    if row is None:
        return ImportResult("failed", "", reason="这份资料不在库里了")
    source_name = str(row["source_name"])
    stored = paths.resolve_stored_path(row["stored_path"]) if row["stored_path"] else None
    if stored is None or not stored.is_file():
        return ImportResult("failed", source_name, reason="找不到副本，无法重建", doc_id=doc_id)

    # 旧树与版本：重建 = 同一行升一个版本（point key 里带 version，新旧树天然共存）。
    old_version = int(row["version"] or 1)
    serving = str(row["status"]) in ("ready", "failed")
    new_version = old_version + 1
    old_child = store.child_keys(doc_id, old_version)
    old_parent = store.parent_keys(doc_id, old_version)

    now = time.time()
    outcome = _ImportRun()
    try:
        # 阶段 A：流式解析→切分→嵌入→写 Qdrant（**无 SQLite 长事务**；
        # 新点全部 pending，检索看不见，旧树照常服务）。
        outcome = _ImportRun()
        _stream_into(
            stored,
            source_name,
            doc_id,
            outcome,
            version=new_version,
            chunk_status="pending",
            embed=embed,
        )
        if outcome.groups == 0 or not outcome.text.strip():
            raise _NothingToIndex(outcome.notes[0] if outcome.notes else "")
    except (JobCancelled, JobInterrupted, KeyboardInterrupt):
        # 取消/退出不是失败：删掉本次半截候选点即可（旧树无损，行状态不动）。
        _drop_version_points(doc_id, new_version)
        raise
    except _NothingToIndex as nothing:
        _drop_version_points(doc_id, new_version)
        store.mark_failed(doc_id, f"重建失败：{nothing.reason or '没有可检索的文字'}")
        return ImportResult("failed", source_name, reason="重建失败：切不出可检索的片段", doc_id=doc_id)
    except (ParseError, OSError) as exc:
        _drop_version_points(doc_id, new_version)
        store.mark_failed(doc_id, f"重建失败：{exc}")
        return ImportResult("failed", source_name, reason=f"重建失败：{exc}", doc_id=doc_id)
    except Exception as exc:
        # 未预料异常：旧树无损（这是与旧实现的本质区别——不再"先删后建"）。
        logger.exception("重建失败（doc {}）：{}", doc_id, exc)
        _drop_version_points(doc_id, new_version)
        store.mark_failed(doc_id, f"重建失败：{exc}")
        return ImportResult("failed", source_name, reason=f"重建失败：{exc}", doc_id=doc_id)

    # 阶段 B：揭示。一次 set_payloads 同时翻两组（合并调用，避免"旧已退场、新未上岗"窗口）；
    # Qdrant 调用**在 SQLite 事务外**——它不可回滚，放事务里只会制造不一致。
    # 资料可能在流式阶段被删除：先确认行还在，别把孤儿点标成 active/去改不存在的行。
    if store.get(int(doc_id)) is None:
        _drop_version_points(doc_id, new_version)
        raise JobCancelled("资料已被删除，放弃重建揭示")

    from app.core.vector import api

    new_child = store.child_keys(doc_id, new_version)
    new_parent = store.parent_keys(doc_id, new_version)
    text_hash = _fingerprint(outcome.text)
    if serving:
        api.set_payloads(
            api.COLLECTION_CHUNKS,
            [(new_child, {"status": "active"}), (old_child, {"status": "superseded"})],
        )
        api.set_payloads(
            api.COLLECTION_PARENTS,
            [(new_parent, {"status": "active"}), (old_parent, {"status": "superseded"})],
        )
    else:
        # 候选版本（还没在服）：旧候选点被新版取代，直接删；新点保持 pending，
        # 状态机走 indexing →（rebuild_vectors/后续流程）→ ready，与旧行为一致。
        api.delete_keys(api.COLLECTION_CHUNKS, old_child)
        api.delete_keys(api.COLLECTION_PARENTS, old_parent)

    # 阶段 C：短事务提交 DB（version 升级 + 摘要 + 状态）。
    try:
        with transaction() as conn:
            updated = store.finish_document(
                conn,
                doc_id,
                text_hash=text_hash,
                pages=outcome.pages,
                ocr_pages=outcome.ocr_pages,
                chars=len(outcome.text),
                detail=outcome.notes[0] if outcome.notes else None,
                now=now,
            )
            if not updated:
                raise JobCancelled("资料已被删除，放弃重建揭示")
            conn.execute(
                "UPDATE documents SET version = ? WHERE id = ?", (new_version, int(doc_id))
            )
            if serving:
                store.mark_ready(
                    conn,
                    doc_id,
                    text_hash=text_hash,
                    detail=outcome.notes[0] if outcome.notes else None,
                    now=now,
                )
            else:
                conn.execute(
                    "UPDATE documents SET status = 'indexing', detail = '重建完成，待建向量',"
                    " updated_at = ? WHERE id = ?",
                    (now, int(doc_id)),
                )
    except JobCancelled:
        # 行在 Qdrant 切换之后被删：把新版本点清掉；旧点由删除流程的二次清点收尾。
        _drop_version_points(doc_id, new_version)
        raise
    except Exception as exc:
        # payload 已翻、DB 没提交：新旧状态短暂漂移，由 cleanup 的对账自愈收敛（见 maintenance）。
        logger.exception("重建揭示提交失败（doc {}）：{}", doc_id, exc)
        return ImportResult(
            "failed", source_name, reason=f"重建揭示失败：{exc}", doc_id=doc_id, error_kind="io"
        )
    logger.info("已重建：{}（{} 个父块 / {} 个片段，v{} → v{}）",
                source_name, outcome.groups, outcome.rows, old_version, new_version)
    try:
        from app.rag.store import stats as store_stats

        store_stats.clear_caches()
    except Exception:
        pass
    return ImportResult("imported", source_name, doc_id=doc_id, chunks=outcome.rows)


def rebuild_lexical() -> int:
    """**只重算词法**（P1.8 快路径）：按当前词库重算所有子块的 ``tokens``。

    - 不重新解析、不重新切分、不动向量 → 秒级；
    - 改了词库/分词参数后用它，界面上是"只重算词法"那个按钮；
    - 调用方（服务层）负责把当前指纹记为"已按这套建过索引"。

    返回：更新了多少行（没改词库时是 0，界面可以说"没有需要重算的"）。
    """
    updated = store.rebuild_tokens()
    logger.info("词法重算完成：更新 {} 行", updated)
    return updated
