"""知识库相关的**入队动作**（后端功能区，不是任务系统装配处）。

**为什么放这里而不是 `app/tasks/__init__.py`**：``app/tasks/`` 的 ``__init__`` 是任务系统的
装配处（发现任务类、注册 kind、启动/停止执行器）。而 ``enqueue_import`` 这类是**某个功能
在业务上的排队动作**——它知道"导入该用什么优先级""同一份资料别重复排"，这些是知识库的
规则，不是任务系统的规则。放在功能后端（services）里，任务系统才保持"不认识具体功能"。

调用方（界面/其他服务）只认这里；底层仍然是 :func:`app.core.jobs.api.enqueue`。
"""

from __future__ import annotations

import json
import time
import uuid

from loguru import logger

from app.core.jobs import api
from app.core.jobs.model import PRIORITY_DEFAULT, PRIORITY_WAITING


def enqueue_import(
    payload: dict[str, str],
    *,
    dedupe_key: str,
    user_waiting: bool = True,
    run_id: str | None = None,
) -> int:
    """排一条导入任务（用户在等 → 优先级最高，别让它排在后台活后面）。

    唤醒由 `app.core.jobs.api.enqueue` 负责（它认识本进程的调度器），这里不用重复做。
    返回：任务 id（去重命中时是已有那条的 id）。
    """
    # 函数内懒加载：任务模块会 import 回本服务（rebuild → knowledge），
    # 模块顶层互相 import 会成环（注册任务时串起来）。kind 用 `.key()` 保持单一事实源。
    from app.tasks.ingest import Ingest

    return api.enqueue(
        Ingest.key(),
        payload,
        dedupe_key=dedupe_key,
        priority=PRIORITY_WAITING if user_waiting else PRIORITY_DEFAULT,
        run_id=run_id,
    )


def enqueue_reindex(doc_ids: list[int] | None = None) -> int:
    """排一条"全量重建"任务（**同一时刻只排一条**：dedupe_key 固定）。

    为什么去重：全量重建是小时级的活，用户多点几次不该排出一串重复任务。
    """
    from app.tasks.reindex import Reindex

    payload: dict[str, object] = {} if doc_ids is None else {"doc_ids": [int(i) for i in doc_ids]}
    return api.enqueue(Reindex.key(), payload, dedupe_key="reindex")


def _ensure_rebuildable(doc_id: int) -> None:
    """校验资料存在且有副本；不满足抛 ValueError（不排必败任务）。"""
    from app.core import paths
    from app.rag import kb_store

    row = kb_store.get_document(int(doc_id))
    if row is None:
        raise ValueError("这份资料已经不在库里了（可能已被删除）")
    stored = str(row["stored_path"] or "")
    copy = paths.resolve_stored_path(stored) if stored else None
    if copy is None or not copy.is_file():
        raise ValueError("这份资料还没有可用副本（导入未完成或已失败），请删除后重新导入")


def enqueue_rebuild(doc_id: int, *, run_id: str | None = None) -> int:
    """排一条"重建索引"任务（用副本重新解析与切分）。

    **为什么它必须走队列**：重建要重新解析整份文件（大 PDF 好几秒），以前是在界面回调里
    同步做的——那会卡住界面（架构里最不该出现的"同步重活"）。走队列之后进度在任务页可见，
    关窗口也不丢。

    异常：
        ValueError: 资料不存在、或没有可用副本（导入未完成/失败后副本已丢）。
            这里**提前拒绝**，不排一条注定失败的任务（旧行为是排进去再报
            "找不到副本，无法重建"）。
    """
    from app.tasks.rebuild import Rebuild

    _ensure_rebuildable(doc_id)
    return api.enqueue(
        Rebuild.key(),
        {"doc_id": int(doc_id)},
        dedupe_key=f"rebuild:doc:{int(doc_id)}",  # 同一份资料不重复排
        priority=PRIORITY_WAITING,
        run_id=run_id,
    )


def enqueue_rebuild_many(doc_ids: list[int]) -> int:
    """批量重建（同一份资料排两次会被 dedupe 合成一条）。返回排进去的条数。

    整批共享一个 ``run_id``：失败后可 ``store.requeue_run(run_id)`` 整批重排。
    **先全部校验再入队**：避免"前几份已排进去、后面一份没副本整批报错"的半截状态。
    """
    unique = sorted({int(doc_id) for doc_id in doc_ids})
    for doc_id in unique:
        _ensure_rebuildable(doc_id)
    run_id = uuid.uuid4().hex
    return len({enqueue_rebuild(doc_id, run_id=run_id) for doc_id in unique})


def enqueue_backfill_attrs() -> int:
    """v1 已移除动态字段（attrs）数据面；保留入口返回 0，界面不会排空任务。"""
    return 0


def migrate_server_store_if_needed() -> int:
    """服务端库为空、但 SQLite 里已有就绪资料时，自动排一轮重建。

    为什么：本地嵌入式切服务端时，``data/qdrant`` 的向量不会出现在
    ``data/qdrant-server`` 里；不重建的话"资料在、搜索为空"。这一步只做一次
    （``settings.vector.server_migrated``），之后由用户自己控制重建。

    返回：排进队列的条数（0 表示不需要/已迁移）。
    """
    from app.core.config import store as params_store

    try:
        if not str(params_store.load()["vector.url"] or "").strip():
            return 0
    except Exception:
        return 0

    from app.integrations import qdrant as qi

    try:
        client = qi.qdrant_client()
        qi.ensure_collection(client)
        if int(client.count(collection_name=qi.COLLECTION).count) > 0:
            return 0
    except Exception as exc:
        logger.warning("检查向量库是否为空失败（跳过自动迁移）：{}", exc)
        return 0

    from app.core.db.connection import get_connection, transaction

    try:
        conn = get_connection()
        if conn.execute(
            "SELECT 1 FROM settings WHERE key = 'vector.server_migrated'"
        ).fetchone():
            return 0
        rows = conn.execute(
            "SELECT id FROM documents WHERE status = 'indexed' AND stored_path <> ''"
        ).fetchall()
    except Exception as exc:
        logger.warning("读取待迁移资料失败（跳过自动迁移）：{}", exc)
        return 0
    doc_ids = [int(row[0]) for row in rows]
    if not doc_ids:
        return 0

    try:
        count = enqueue_rebuild_many(doc_ids)
    except Exception as exc:
        # 有一份没副本会整批拒绝：退化为逐份排，能排的排。
        logger.warning("批量排重建失败，改为逐份：{}", exc)
        count = 0
        for doc_id in doc_ids:
            try:
                enqueue_rebuild(doc_id)
                count += 1
            except Exception as one:
                logger.warning("资料 #{} 无法自动重建：{}", doc_id, one)
    if count:
        try:
            with transaction() as writer:
                writer.execute(
                    "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
                    " updated_at = excluded.updated_at",
                    ("vector.server_migrated", json.dumps(time.time()), time.time()),
                )
        except Exception as exc:
            logger.warning("记录服务端迁移标记失败（下次可能重复排）：{}", exc)
        logger.info("服务端向量库为空：已自动排 {} 份重建（首次切换迁移）", count)
    return count
