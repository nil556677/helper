"""知识库生命周期（v1）：删除、重建、重新向量化。"""

from __future__ import annotations

from loguru import logger

from app.core import paths
from app.rag import kb_store

from ._shared import RebuildOutcome


def delete(doc_ids: list[int]) -> int:
    """删掉若干份资料（SQLite 级联 + Qdrant 按 doc_id 删点 + 副本文件）。返回实际删除条数。"""
    from app.integrations import qdrant as qi

    removed = 0
    for doc_id in {int(item) for item in doc_ids}:
        row = kb_store.get_document(doc_id)
        if row is None:
            continue
        try:
            qi.delete_document_points(qi.qdrant_client(), doc_id)
        except Exception as exc:
            logger.warning("删除资料 {} 的向量失败：{}", doc_id, exc)
        kb_store.delete_document(doc_id)
        _remove_copy(str(row["stored_path"] or ""))
        removed += 1
    return removed


def _remove_copy(stored_path: str) -> None:
    """删除 data/files 下的副本；只认 files 目录内的文件，避免误删。"""
    if not stored_path:
        return
    copy = paths.resolve_stored_path(stored_path)
    if copy is None or not copy.is_file():
        return
    try:
        root = paths.files_dir().resolve()
        resolved = copy.resolve()
        if not resolved.is_relative_to(root):
            logger.warning("副本不在 data/files 内，未删除：{}", copy)
            return
        resolved.unlink(missing_ok=True)
        with __import__("contextlib").suppress(OSError):
            resolved.parent.rmdir()
    except OSError as exc:
        logger.warning("删除副本失败（{}）：{}", copy, exc)


def rebuild(doc_id: int) -> RebuildOutcome:
    """用副本重建某份资料（走新 ingest pipeline）。"""
    row = kb_store.get_document(int(doc_id))
    if row is None:
        return RebuildOutcome(False, "这份资料已经不在库里了", 0)
    stored = str(row["stored_path"] or "")
    copy = paths.resolve_stored_path(stored) if stored else None
    if copy is None or not copy.is_file():
        return RebuildOutcome(False, "这份资料没有可用副本，请删除后重新导入", 0)
    from app.ingest import import_stored

    outcome = import_stored(copy, name=str(row["name"]), claim_doc_id=int(doc_id))
    return RebuildOutcome(
        ok=outcome.status == "imported",
        detail=outcome.reason,
        chunks=outcome.chunks,
    )


def revectorize(doc_ids: list[int]) -> int:
    """批量重新向量化（按当前参数重建索引）。"""
    from .queue import enqueue_rebuild

    unique = {int(doc_id) for doc_id in doc_ids}
    return len({enqueue_rebuild(int(doc_id)) for doc_id in unique})


def rollback_to_version(version_doc_id: int) -> str:
    """v1 不做历史版本链；重建即替换当前版本。"""
    del version_doc_id
    return "当前版本不支持回滚历史版本；如需重新生成请用「重建索引」。"
