"""版本切换与回滚（**只保留这两件事**）。

历史：这里曾是"把一份资料向量化"的独立任务（``embed_document`` + 断点续跑 cursor）。
单遍 ingest 落地后（``importer.import_path`` 解析→切分→嵌入一次完成），那个任务已被
``app/tasks/ingest.py`` 取代并从注册表移除。相关函数**已删除**——尤其是它的
"cursor 每批推进、向量却要等会话退出才落盘"的组合，恢复旧路径就等于恢复一个
"崩溃后跳过一批、向量永久缺失"的数据丢失隐患。

现在只保留：

- :func:`rollback_to` —— 回滚到某个历史版本（UI 的"回滚"按钮）；
- :func:`switch_version` —— 对外的切换入口（运维/重跑收尾）；
- :func:`_switch_to_new_version` —— 两者共用的原子切换。

**切换顺序（别改）**：一次 ``set_payloads`` 同时翻新旧两版的 payload，再落 DB 状态。
分两次写会留下"旧已退场、新未上岗"的窗口（检索只认 ``status=active``，两版都看不见）。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from app.core.db.connection import transaction
from app.core.jobs.model import JobError
from app.core.vector import api as vector_api
from app.core.vector.keys import make_key, parse_key
from app.rag import store

#: 向量 key 的形状**只有一处定义**（见 :mod:`app.core.vector.keys`）；这里只是给本模块一个名字。
vector_key = make_key
parse_vector_key = parse_key

#: 状态常量（与 documents.status / Qdrant payload 的 status 一致）
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"


@dataclass(frozen=True)
class EmbedReport:
    """一次版本切换/回滚的结果（给界面一句人话）。"""

    doc_id: int
    version: int
    total: int
    done: int
    skipped: int
    switched: bool = False
    superseded_doc_id: int | None = None
    #: 退场的那个是**曾上过线**的版本（False = 只是候选作废，从未服务过）
    superseded_was_live: bool = True

    @property
    def note(self) -> str:
        """一句话结果。"""
        base = f"向量化 {self.done}/{self.total} 块"
        if self.skipped:
            base += f"（续跑，跳过 {self.skipped} 块）"
        if self.switched:
            base += f"；已切换到新版本 #{self.doc_id}"
            if self.superseded_doc_id:
                base += f"（旧版本 #{self.superseded_doc_id} 退场）"
        return base


def _switch_to_new_version(old_doc_id: int, new_doc_id: int, new_version: int) -> None:
    """**原子切换**：向量 payload 先翻（一次调用打两组补丁），再提交数据库事务。

    顺序不能反：数据库先翻而向量那步失败，就会出现"数据库说新版本已就位、
    检索却一条也看不见"的空窗；反过来最坏只是**短暂地新旧都可见**（下一轮会自愈）。
    """
    old_row = store.get(old_doc_id)
    old_version = int(old_row["version"] or 1) if old_row is not None else 1  # type: ignore[index]
    collection = vector_api.COLLECTION_CHUNKS

    new_keys = store.child_keys(new_doc_id, new_version)
    old_keys = store.child_keys(old_doc_id, old_version)
    # **一次调用打两组补丁**：避免"旧已退场、新还没上岗"的不可见窗口
    vector_api.set_payloads(
        collection,
        [
            (new_keys, {"status": STATUS_ACTIVE}),
            (old_keys, {"status": STATUS_SUPERSEDED}),
        ],
    )

    with transaction() as conn:
        store.switch_version(conn, old_doc_id, new_doc_id)
        # **候选版本一并作废**（P1 审查 H2）：被更高版本"超车"的候选永远不会被服务，
        # 也不清理的话会一直占覆盖率分母和存储（实测：覆盖率永远停在 33%）。
        store.supersede_stale_candidates(
            conn, str(old_row["doc_key"] or ""), before_version=new_version
        )


def rollback_to(target_doc_id: int) -> EmbedReport:
    """**回滚到某个历史版本**（P1.7 说的"可回滚"的落地点）。

    做法就是把切换反过来跑：目标版本重新上位、当前服务版本退场。
    片段与向量 payload 一起翻，所以检索立刻只看回滚后的那一版。

    参数：
        target_doc_id: 要回到的那个版本（必须是**已就绪或已退场**的同 doc_key 版本）。
    返回：:class:`EmbedReport`（``switched=True``，``superseded_doc_id`` 是退场的那个）。
    异常：
        JobError: 目标不存在 / 还没建完向量（``not_found`` / ``bad_request``，都不重试）。
    """
    target = store.get(target_doc_id)
    if target is None:
        raise JobError(f"资料 #{target_doc_id} 不存在", kind="not_found", retryable=False)
    key = str(target["doc_key"] or "")
    current = store.current_row_by_key(key) if key else None
    if current is None or int(current["id"]) == int(target_doc_id):
        raise JobError(
            f"资料 #{target_doc_id} 已经是当前版本，或没有可退场的版本",
            kind="bad_request",
            retryable=False,
        )
    if str(target["status"]) not in (STATUS_SUPERSEDED, "ready"):
        raise JobError(
            f"资料 #{target_doc_id} 还没建完向量（状态 {target['status']}），不能回滚到它",
            kind="bad_request",
            retryable=False,
        )
    old_id = int(current["id"])
    version = int(target["version"] or 1)
    _switch_to_new_version(old_id, int(target_doc_id), version)
    logger.info("已回滚：资料 #{}（v{}）上位，旧版本 #{} 退场", target_doc_id, version, old_id)
    return EmbedReport(
        doc_id=int(target_doc_id),
        version=version,
        total=len(store.child_rows(int(target_doc_id), version)),
        done=0,
        skipped=0,
        switched=True,
        superseded_doc_id=old_id,
    )


def switch_version(old_doc_id: int, new_doc_id: int) -> None:
    """对外的切换入口（"重跑一次收尾"或运维手工切换时用）。"""
    row = store.get(new_doc_id)
    if row is None:
        raise JobError(f"资料 #{new_doc_id} 不存在", kind="not_found", retryable=False)
    _switch_to_new_version(old_doc_id, new_doc_id, int(row["version"] or 1))  # type: ignore[index]
