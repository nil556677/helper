"""知识库的生命周期操作（删除、重建、重新向量化、回滚到版本）。"""

from __future__ import annotations

from app.rag.ingest import importer, rebuilder

from ._shared import RebuildOutcome


def delete(doc_ids: list[int]) -> int:
    """删掉若干份资料（连带片段与索引）。返回实际删除条数。"""
    return importer.delete_documents(doc_ids)


def rebuild(doc_id: int) -> RebuildOutcome:
    """用副本重建某份资料的片段（换了切分参数或解析器升级后用）。"""
    result = rebuilder.rebuild_document(doc_id)
    return RebuildOutcome(
        ok=result.status == "imported",
        detail=result.reason,
        chunks=result.chunks,
    )


def revectorize(doc_ids: list[int]) -> int:
    """**批量重新向量化**：给选中的资料各排一条 ``rebuild`` 任务，返回排了几条。

    为什么走 ``rebuild`` 而不是 ``ingest``：``ingest`` 的参数是**上传暂存路径**
    （``Ingest.Params.path`` 必填），而这里只有资料 id——拿不到 inbox 里的那一次上传。
    正确做法是用**已留存的副本**重新解析+重嵌（``rebuild_document`` 单遍完成，见
    ``app/tasks/rebuild.py``），语义上就是"按当前参数重建这份资料的索引"，且天然幂等。
    """
    from .queue import enqueue_rebuild

    unique = {int(doc_id) for doc_id in doc_ids}
    return len({enqueue_rebuild(int(doc_id)) for doc_id in unique})


def rollback_to_version(version_doc_id: int) -> str:
    """**回滚到某个历史版本**（P1.7 的"可回滚"落地点），返回一句结果给界面。

    为什么放在服务层：界面不能直接 import core（分层红线）；而且"回滚"是业务动作，
    它的前置校验（目标必须已就绪/已退场）属于服务层，不属于按钮回调。
    """
    from app.rag.ingest import version_switch

    report = version_switch.rollback_to(int(version_doc_id))
    return report.note
