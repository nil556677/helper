"""维护工具（cleanup 用的那几件事）：孤儿副本、inbox 残留、WAL 合并。

**任务入口不在这里**：``cleanup`` 的 handler 搬到了 ``app/tasks/cleanup.py``
（每个任务一个模块，自带 ``KIND`` 声明，启动时自动发现）。本模块只留**可单独调用、可单测**
的维护动作。

**三条安全线**（都不是随手定的）：

1. **只碰"十分钟没动过"的孤儿副本**：正在导入的资料是"副本已落盘、资料行还没提交"，
   这时它在库里查不到——不等一等就会把正在用的副本删掉；
2. **inbox 里还被任务引用的文件绝不删**：那条任务马上就要读它；
3. **WAL 用 TRUNCATE 合并回主库**：这样"备份 = 复制 data/"才是完整的（§9）。

每个动作都**幂等**：处理的都是"没人认领"的东西，重跑结果一样。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import time
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.db.connection import get_connection
from app.core.jobs import api
from app.rag import store

#: 孤儿副本的"最小年龄"：十分钟内动过的一律不碰（可能正在导入）
ORPHAN_MIN_AGE_S = 600.0

#: inbox 残留的"最小年龄"：一小时内写进来的一律不碰
INBOX_MIN_AGE_S = 3600.0


def enqueue_cleanup(reason: str = "manual") -> int:
    """排一条清理任务（孤儿副本 / inbox 残留 / WAL / 历史 / 过期日志）。

    **去重**：`dedupe_key="cleanup"` 保证同一时刻只有一条在队列里——
    启动一次就排一条的话，用户频繁开关窗口会把队列塞满维护任务。

    这是**维护功能自己的入队动作**（不是任务系统装配处的职责）：任务系统只提供
    "队列"这个通用设施，具体"什么时候该清什么"由各功能决定。
    """
    from app.core.jobs.model import PRIORITY_MAINTENANCE
    from app.tasks.cleanup import Cleanup

    return api.enqueue(
        Cleanup.key(),
        {"reason": reason},
        dedupe_key="cleanup",
        priority=PRIORITY_MAINTENANCE,
    )


def known_copy_dirs() -> set[str]:
    """库里还引用着的副本目录名（`data/files/<hash12>/` 的第一段）。"""
    names: set[str] = set()
    for stored in store.all_stored_paths():
        first = stored.replace("\\", "/").split("/")[0]
        if first:
            names.add(first)
    return names


def referenced_inbox_files() -> set[str]:
    """还被**待跑/在跑**任务引用着的暂存文件（那些不能删）。

    返回集合同时含**文件名**与**解析后的绝对路径**两种形态（删除侧两种都查，
    防止大小写/短路径差异漏配）。**不做 limit 截断**：截断会让第 N+1 条任务的
    暂存文件被清理任务删掉——那是静默的数据丢失（清理是幂等的，宁可多扫）。
    """
    names: set[str] = set()
    for job in api.list_jobs(statuses=("pending", "running"), limit=100000):
        raw = job.payload.get("path")
        if not raw:
            continue
        names.add(Path(str(raw)).name)
        with contextlib.suppress(OSError):
            names.add(str(Path(str(raw)).resolve()))
    return names


def remove_orphan_copies(*, now: float | None = None, min_age_s: float = ORPHAN_MIN_AGE_S) -> int:
    """删掉没人引用的副本目录，返回删了几个。"""
    folder = paths.files_dir()
    if not folder.is_dir():
        return 0
    stamp = time.time() if now is None else now
    keep = known_copy_dirs()
    removed = 0
    for child in sorted(folder.iterdir()):
        if not child.is_dir() or child.name in keep:
            continue
        try:
            if stamp - child.stat().st_mtime < min_age_s:
                continue  # 刚写进来的：很可能正在导入（副本已落盘、资料行还没提交）
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
            logger.info("清理孤儿副本：{}", child.name)
        except OSError as exc:
            logger.warning("删孤儿副本失败（{}）：{}", child, exc)
    return removed


def remove_inbox_leftovers(*, now: float | None = None, min_age_s: float = INBOX_MIN_AGE_S) -> int:
    """删掉 inbox 里既没人引用、又放久了的暂存文件，返回删了几个。"""
    folder = paths.inbox_dir()
    if not folder.is_dir():
        return 0
    stamp = time.time() if now is None else now
    keep = referenced_inbox_files()
    removed = 0
    for child in sorted(folder.iterdir()):
        if not child.is_file():
            continue
        try:
            resolved = str(child.resolve())
        except OSError:
            resolved = ""
        if child.name in keep or resolved in keep:
            continue
        try:
            if stamp - child.stat().st_mtime < min_age_s:
                continue
            child.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            logger.warning("删暂存文件失败（{}）：{}", child, exc)
    return removed


#: 退场版本的"回滚窗口"：6 小时内还能 `rollback_to`，过了才物理删除
SUPERSEDED_TTL_S = 6 * 3600.0


def purge_superseded(*, older_than_s: float = SUPERSEDED_TTL_S, now: float | None = None) -> dict[str, int]:
    """**物理删除到期退场版本**（P1.9）：向量 → 行 → 副本，顺序不能反。

    为什么要删：版本链留旧行是为了**可回滚**，不是长期保存——旧版本的片段与向量
    会一直占空间，也会让"对账"永远报孤儿。

    返回：``{"versions": 删了几版, "vectors": 删了几条向量, "copies": 删了几个目录}``。

    安全线：
    - **只删 `superseded`**（正在服务/正在建的版本绝不碰）；
    - 删副本前确认**没有别的资料行**共用同一个哈希目录（同内容多版本会共用）；
    - 幂等：向量/副本已经不在也不报错。
    """
    from app.core.vector import api as vector_api

    stamp = time.time() if now is None else now
    rows = store.superseded_rows(older_than_s, now=stamp)
    if not rows:
        return {"versions": 0, "vectors": 0, "copies": 0}

    vectors = 0
    try:
        for row in rows:
            keys = store.child_keys(int(row["id"]), int(row["version"] or 1))
            parent_keys = store.parent_keys(int(row["id"]), int(row["version"] or 1))
            # 顺序一：先删向量（行还在时删，最多"行在、向量没了"；反过来会留下看不见的向量）
            # **两个集合都要清**：退场版本的 parents 无向量点也会残留（P7 审查）。
            deleted = vector_api.delete_keys(vector_api.COLLECTION_CHUNKS, keys)
            deleted_parents = vector_api.delete_keys(vector_api.COLLECTION_PARENTS, parent_keys)
            vectors += int(deleted.get("qdrant", 0)) + int(deleted_parents.get("qdrant", 0))
    except Exception as exc:  # 向量库暂不可用：整轮跳过，别只删行留下孤儿向量
        logger.warning("清理退场版本跳过（向量库暂不可用）：{}", exc)
        return {"versions": 0, "vectors": 0, "copies": 0}

    candidates: list[str] = []
    for row in rows:
        stored = str(row["stored_path"] or "")
        if stored:
            candidates.append(stored)
    store.delete_many([int(row["id"]) for row in rows])  # 顺序二：删行（片段级联）

    removed_dirs = 0
    in_use = store.paths_in_use()
    for stored in candidates:
        resolved = paths.resolve_stored_path(stored)
        if resolved is None:
            continue  # 越界/异常路径：不碰
        folder = resolved.parent
        if not folder.is_dir():
            continue
        prefix = folder.name
        # 同内容多版本共用 `files/<hash12>/`：还有人在用就不删
        if any(path.startswith(prefix + "/") for path in in_use):
            continue
        shutil.rmtree(folder, ignore_errors=True)  # 顺序三：最后删副本
        removed_dirs += 1

    logger.info(
        "清理退场版本：{} 版 / {} 条向量 / {} 个副本目录", len(rows), vectors, removed_dirs
    )
    return {"versions": len(rows), "vectors": vectors, "copies": removed_dirs}


#: 占位行的"认领超时"：超过 24 小时仍是 pending 且没有任何待处理任务认领 → 判为孤儿
PENDING_TTL_S = 24 * 3600.0


def remove_stale_pending(*, older_than_s: float = PENDING_TTL_S, now: float | None = None) -> int:
    """清掉**孤儿占位行**（P1.16 的收尾）：超时仍是 pending、且没有任务会来认领它。

    什么时候会产生：上传后落了占位行，但导入任务丢了（应用崩溃、手动清任务表）。
    不清理的话，列表里会永远挂着一份"待处理"、永远不会动的资料。

    **安全线**：只要还有 pending/running 的导入任务**引用它**（payload 里的 ``doc_id``），
    就绝不删——那条任务可能下一秒就开始跑。
    """
    stamp = time.time() if now is None else now
    claimed: set[int] = set()
    for row in get_connection().execute(
        "SELECT payload FROM jobs WHERE kind = 'ingest' AND status IN ('pending', 'running')"
    ).fetchall():
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        doc_id = payload.get("doc_id")
        if doc_id is not None:
            claimed.add(int(doc_id))

    rows = get_connection().execute(
        "SELECT id FROM documents WHERE status = 'pending' AND created_at <= ? ORDER BY id",
        (stamp - float(older_than_s),),
    ).fetchall()
    stale = [int(row["id"]) for row in rows if int(row["id"]) not in claimed]
    if not stale:
        return 0
    store.delete_many(stale)
    logger.info("清理孤儿占位行：{} 行（超时 {}s 且无任务认领）", len(stale), int(older_than_s))
    return len(stale)

def reconcile() -> dict[str, object]:
    """**三方对账**（documents ↔ chunks ↔ vectors）：报告孤儿与缺失，不自动修。

    返回：
        ``{"vector_orphans": [...], "vector_missing": [...], "documents_without_chunks": [...],
        "orphan_copies": [...]}``（各取前 20 条样本 + ``counts`` 汇总）。

    为什么要对账：崩溃/半途失败/手工删库都会让三方漂移。**不自动修**——自动删向量
    可能删掉正在服务的版本，那比"报出来"危险得多。
    """
    # **两个口径修正**（P1 审查 H3 / M1）：
    # - 孤儿 = 向量有、但**任何资料行（含退场版本）都不认领**。退场版本的向量在 6 小时
    #   回滚窗口内合法存在；只看在册版本会把窗口内的向量全报成孤儿（实测：切 4 版报 3 个孤儿）；
    # - "无片段资料"排除 pending 占位行——排队中的行本来就没有片段，报出来是噪音。
    try:
        live = store.live_child_keys()
        claimed = store.all_child_keys() | store.all_parent_keys()
        stored = store.stored_vector_keys() | store.stored_parent_keys()
    except Exception as exc:
        # Qdrant/后端不可用：对账是诊断工具，报错误标记而不是让整页 500（异常审查）
        # counts 也要给**完整形状**（全 0）：调用方（cleanup 任务、界面）按固定键取值，
        # 缺键会 KeyError（实测：启动早期 Qdrant 未就绪时 cleanup 任务因此崩溃）。
        logger.warning("对账失败（Qdrant/后端不可用）：{}", exc)
        return {
            "error": str(exc),
            "counts": {
                "vector_orphans": 0,
                "vector_missing": 0,
                "documents_without_chunks": 0,
                "orphan_copies": 0,
            },
            "vector_orphans": [],
            "vector_missing": [],
            "documents_without_chunks": [],
            "orphan_copies": [],
        }

    orphans = sorted(stored - claimed)
    missing = sorted(live - stored)

    # "无片段资料"：在册文档里，Qdrant 中没有任何 child 点的那些。片段已不进 SQLite，
    # 靠 claimed 集合（Qdrant child key）反推有片段的资料。
    with_children = {
        int(key.split(":")[0]) for key in claimed if ":" in key and key.split(":")[0].isdigit()
    }
    rows = get_connection().execute(
        "SELECT d.id AS id FROM documents d WHERE d.status NOT IN ('superseded', 'pending')"
    ).fetchall()
    no_chunks = [int(row["id"]) for row in rows if int(row["id"]) not in with_children]

    in_use = store.paths_in_use()
    used_dirs = {path.split("/")[0] for path in in_use if path}
    orphan_copies: list[str] = []
    folder = paths.files_dir()
    if folder.is_dir():
        orphan_copies = sorted(
            entry.name for entry in folder.iterdir() if entry.is_dir() and entry.name not in used_dirs
        )

    return {
        "counts": {
            "vector_orphans": len(orphans),
            "vector_missing": len(missing),
            "documents_without_chunks": len(no_chunks),
            "orphan_copies": len(orphan_copies),
        },
        "vector_orphans": orphans[:20],
        "vector_missing": missing[:20],
        "documents_without_chunks": no_chunks[:20],
        "orphan_copies": orphan_copies[:20],
    }

def heal_payload_status() -> dict[str, int]:
    """**payload ↔ DB 状态自愈**（P7 审查 H3）：真的修，不只是报。

    为什么要它：版本切换是"先翻 Qdrant payload、后提交 DB"——切换点被杀会留下
    **新旧两版同时 active**（检索重复召回）或"DB 已 ready、payload 还是 pending"
    （检索一条看不见）。旧注释声称"下一轮自愈"，但全库没有任何任务兑现它。

    安全规则（只动**能从 DB 唯一推导出正确状态**的点）：
    1. ``documents`` 行**已删除**的点 = 真孤儿 → **直接删**（行都没了，无人认领、无法回滚）；
    2. 文档 ``ready``：当前版本的点 → ``active``；旧版本的点仍 active → ``superseded``；
    3. 文档非 ready（superseded/pending/indexing/failed）：active 的点 → ``superseded``
       （对检索等效"不可见"，进行中的导入/重建随后会自己翻回来）；
    4. 单次修复量有上限（防首次运行全库重写把清理任务拖死），没修完的下轮继续。

    返回：``{"patched_active": …, "patched_superseded": …, "deleted_orphans": …, "scanned": …}``。
    """
    from app.core.vector import api as vector_api

    MAX_PATCH = 2000
    MAX_DELETE = 2000
    rows = get_connection().execute(
        "SELECT id, version, status FROM documents"
    ).fetchall()
    doc_state = {
        int(row["id"]): (int(row["version"] or 1), str(row["status"])) for row in rows
    }

    to_active: dict[str, list[str]] = {}
    to_superseded: dict[str, list[str]] = {}
    orphan_ids: dict[str, list[int]] = {}
    scanned = 0
    empty_result = {
        "patched_active": 0,
        "patched_superseded": 0,
        "deleted_orphans": 0,
        "scanned": 0,
    }
    for collection in (vector_api.COLLECTION_CHUNKS, vector_api.COLLECTION_PARENTS):
        try:
            points = list(vector_api.iter_points(collection))
        except Exception as exc:  # 向量库被占/连不上：自愈是尽力而为，跳过并说明
            logger.warning("payload 状态自愈跳过（向量库暂不可用）：{}", exc)
            return {**empty_result, "error": str(exc)}
        for point in points:
            scanned += 1
            payload = point["payload"]
            doc_id = int(payload.get("documents_id", -1))
            key = str(payload.get("key") or "")
            status = str(payload.get("status") or "")
            version = int(payload.get("version", -1))
            state = doc_state.get(doc_id)
            if state is None:
                if len(orphan_ids.get(collection, [])) < MAX_DELETE:
                    orphan_ids.setdefault(collection, []).append(point["id"])
                continue
            doc_version, doc_status = state
            if doc_status == "ready":
                if status != "active" and version == doc_version and key:
                    to_active.setdefault(collection, []).append(key)
                elif status == "active" and version < doc_version and key:
                    to_superseded.setdefault(collection, []).append(key)
            elif status == "active" and key:
                # 非 ready 文档不该有 active 点（切换中途被杀 / 删除半途失败）
                to_superseded.setdefault(collection, []).append(key)
            if (
                len(to_active.get(collection, [])) >= MAX_PATCH
                or len(to_superseded.get(collection, [])) >= MAX_PATCH
            ):
                break  # 本轮修满，剩下的下轮继续（cleanup 周期跑）

    patched_active = patched_superseded = deleted = 0
    try:
        for collection, groups in (
            (vector_api.COLLECTION_CHUNKS, (to_active, to_superseded)),
            (vector_api.COLLECTION_PARENTS, (to_active, to_superseded)),
        ):
            keys = groups[0].get(collection, [])
            if keys:
                vector_api.set_payload(collection, keys, {"status": "active"})
                patched_active += len(keys)
            keys = groups[1].get(collection, [])
            if keys:
                vector_api.set_payload(collection, keys, {"status": "superseded"})
                patched_superseded += len(keys)
            ids = orphan_ids.get(collection, [])
            if ids:
                deleted += vector_api.delete_points(collection, ids)
    except Exception as exc:  # 写入阶段失败同样降级，别让清理任务抛 traceback
        logger.warning("payload 状态自愈写入失败（已跳过剩余）：{}", exc)
        return {**empty_result, "error": str(exc)}

    if patched_active or patched_superseded or deleted:
        logger.warning(
            "payload 状态自愈：翻 active {} · 翻 superseded {} · 删孤儿 {}（扫描 {} 点）",
            patched_active, patched_superseded, deleted, scanned,
        )
    return {
        "patched_active": patched_active,
        "patched_superseded": patched_superseded,
        "deleted_orphans": deleted,
        "scanned": scanned,
    }


def checkpoint_wal() -> bool:
    """把 WAL 合并回主库（让"只拷 helper.db"的备份也完整）。失败只告警。

    **必须看返回值**：`wal_checkpoint(TRUNCATE)` 在有其它连接读/写时返回 busy 且不做任何事。
    以前这里无条件返回 True，于是任务详情里显示"WAL 已合并"而实际没合并——备份会丢数据。
    """
    from app.core.db.connection import wal_checkpoint

    try:
        ok = wal_checkpoint(get_connection())
    except Exception as exc:
        logger.warning("WAL 合并失败：{}", exc)
        return False
    if not ok:
        logger.warning("WAL 合并未完成（数据库正被其它连接使用）")
    return ok

