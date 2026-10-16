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

import shutil
import sqlite3
import time
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.db.connection import get_connection
from app.core.features.rag.ingest import store
from app.core.jobs import api

#: 孤儿副本的"最小年龄"：十分钟内动过的一律不碰（可能正在导入）
ORPHAN_MIN_AGE_S = 600.0

#: inbox 残留的"最小年龄"：一小时内写进来的一律不碰
INBOX_MIN_AGE_S = 3600.0


def known_copy_dirs() -> set[str]:
    """库里还引用着的副本目录名（`data/files/<hash12>/` 的第一段）。"""
    names: set[str] = set()
    for stored in store.all_stored_paths():
        first = stored.replace("\\", "/").split("/")[0]
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
        if not child.is_file() or child.name in keep:
            continue
        try:
            if stamp - child.stat().st_mtime < min_age_s:
                continue
            child.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
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

