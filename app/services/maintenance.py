"""维护任务（cleanup）：孤儿副本、inbox 残留、WAL、历史清理。

**为什么做成一条任务而不是"启动时顺手做"**：进度、取消、失败重试、关窗口不丢都是任务系统
白送的；而且它是 **`priority=-10` 的维护类**任务，正好验证这一类（绝不和用户在等的活抢 CPU）。

**三条安全线**（都不是随手定的）：

1. **只碰"十分钟没动过"的孤儿副本**：正在导入的资料是"副本已落盘、资料行还没提交"，
   这时它在库里查不到——不等一等就会把正在用的副本删掉；
2. **inbox 里还被任务引用的文件绝不删**：那条任务马上就要读它；
3. **WAL 用 TRUNCATE 合并回主库**：这样"备份 = 复制 data/"才是完整的（§9）。

整个 action **幂等**：删的都是"没人认领"的东西，重跑结果一样。
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.db.connection import get_connection
from app.core.jobs import api, store
from app.core.jobs.model import JobContext
from app.core.logging import purge_old_logs

#: 孤儿副本的"最小年龄"：十分钟内动过的一律不碰（可能正在导入）
ORPHAN_MIN_AGE_S = 600.0

#: inbox 残留的"最小年龄"：一小时内写进来的一律不碰
INBOX_MIN_AGE_S = 3600.0

#: 清理分几步（进度按步报，界面上能看出卡在哪一步）
STEPS = 5


def known_copy_dirs() -> set[str]:
    """库里还引用着的副本目录名（`data/files/<hash12>/` 的第一段）。"""
    rows = get_connection().execute(
        "SELECT stored_path FROM documents WHERE stored_path IS NOT NULL AND stored_path != ''"
    ).fetchall()
    names: set[str] = set()
    for row in rows:
        first = str(row["stored_path"]).replace("\\", "/").split("/")[0]
        if first:
            names.add(first)
    return names


def referenced_inbox_files() -> set[str]:
    """还被**待跑/在跑**任务引用着的暂存文件名（那些不能删）。"""
    names: set[str] = set()
    for job in api.list_jobs(statuses=("pending", "running"), limit=500):
        raw = job.payload.get("path")
        if raw:
            names.add(Path(str(raw)).name)
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
        except OSError as exc:  # noqa: BLE001 - 删不掉不该让整个清理失败
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
        if not child.is_file() or child.name in keep:
            continue
        try:
            if stamp - child.stat().st_mtime < min_age_s:
                continue
            child.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:  # noqa: BLE001 - 同上
            logger.warning("删暂存文件失败（{}）：{}", child, exc)
    return removed


def checkpoint_wal() -> bool:
    """把 WAL 合并回主库（让"只拷 helper.db"的备份也完整）。失败只告警。"""
    try:
        get_connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except sqlite3.Error as exc:
        logger.warning("WAL checkpoint 失败：{}", exc)
        return False


def run_cleanup_job(ctx: JobContext) -> str:
    """清理任务的 handler。"""
    ctx.report(0, STEPS, "找孤儿副本…")
    copies = remove_orphan_copies()

    ctx.report(1, STEPS, "清 inbox 残留…")
    leftovers = remove_inbox_leftovers()

    ctx.report(2, STEPS, "合并 WAL…")
    merged = checkpoint_wal()

    ctx.report(3, STEPS, "清历史记录…")
    purged = store.purge_finished()

    ctx.report(4, STEPS, "清过期日志…")
    logs = purge_old_logs()
    ctx.report(STEPS, STEPS, "完成")

    parts = [
        f"孤儿副本 {copies} 个",
        f"暂存 {leftovers} 个",
        f"历史 {purged} 条",
        f"过期日志 {logs} 个",
    ]
    parts.append("WAL 已合并" if merged else "WAL 跳过")
    return " · ".join(parts)
