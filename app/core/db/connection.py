"""SQLite 连接的唯一入口。

**为什么单独一个模块**：全项目的数据（资料、片段、会话、设置、模型槽位）都在同一个库文件里，
连接参数（WAL / 外键 / busy_timeout / row_factory）必须处处一致，否则会出现
"一半连接开了外键、一半没开"——外键级联删除会在某条路径上静默失效，而这种错查不出来。

**线程模型**：导入与检索会跑在工作线程里，而 sqlite3 连接默认禁止跨线程使用。
这里按线程各持一条连接（``threading.local``），并把事务显式化：连接统一用
``isolation_level=None``（自动提交），要事务就调 :func:`transaction`
——这样 Python 的隐式 BEGIN 不会和我们显式的 ``BEGIN IMMEDIATE`` 打架。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from loguru import logger

from app.core import paths

#: 写锁等待上限：桌面场景下写操作很短，等 5 秒还没拿到锁说明有别的东西出了问题。
DEFAULT_BUSY_TIMEOUT_MS = 5000

#: 线程本地连接（一条连接只在自己的线程里用）。
_local = threading.local()

#: FTS5 可用性只探测一次（它在进程生命周期里不会变）。
_fts5_ok: bool | None = None


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    """设置本项目的连接约定（每次建连都要走这里）。"""
    conn.row_factory = sqlite3.Row
    # WAL：读写并发，写不挡读；synchronous=NORMAL 在 WAL 下已能保证不损坏，换更快的写。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # 外键默认是关的，级联删除全靠它。
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
    return conn


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """新建一条连接（调用方负责关闭；需要复用就用 :func:`get_connection`）。

    参数：
        db_path: 库文件路径；默认 ``{数据目录}/helper.db``。
    返回：配置好的连接（WAL、外键、busy_timeout、Row 工厂）。
    异常：
        sqlite3.Error: 打不开库文件（如目录不可写）。
    """
    target = db_path or paths.db_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：自动提交模式，事务由 transaction() 显式管理。
    conn = sqlite3.connect(str(target), isolation_level=None)
    return _configure(conn)


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """取当前线程的连接（没有就建一条并缓存）。

    参数：
        db_path: 库文件路径；默认 ``{数据目录}/helper.db``。与缓存中的路径不一致时重开。
    返回：当前线程的连接。
    """
    target = db_path or paths.db_file()
    cached = getattr(_local, "conn", None)
    if cached is not None:
        cached_path = getattr(_local, "path", None)
        if cached_path == target:
            return cached
        # 换了库文件（测试里常见）：关掉旧的，别让两条连接指向不同的库。
        cached.close()
    conn = connect(target)
    _local.conn = conn
    _local.path = target
    return conn


@contextmanager
def transaction(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """写事务：``BEGIN IMMEDIATE`` → 交还连接 → 提交；出异常回滚。

    为什么用 ``BEGIN IMMEDIATE``：先把写锁拿到手。若等提交时才发现锁冲突，
    SQLite 只能报 SQLITE_BUSY，而且此时已经执行了一半语句。

    参数：
        db_path: 库文件路径；默认当前线程的连接。
    异常：
        原样抛出块内的异常（回滚后）。
    """
    conn = get_connection(db_path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def close_thread_connection(*, checkpoint_first: bool = False) -> None:
    """关掉当前线程的连接（测试与进程退出路径用）。

    参数：
        checkpoint_first: 关之前先做一次 ``wal_checkpoint(TRUNCATE)``。

    **退出路径必须传 True**：WAL 模式下最近的写入可能只在 ``helper.db-wal`` 里，
    而"备份 = 复制 data/ 目录"虽然能兜住，**只拷 helper.db 就会丢最近的写入**；
    TRUNCATE 一次之后主库文件就是完整的。checkpoint 失败只告警，不挡退出。
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        return
    if checkpoint_first:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            logger.warning("WAL checkpoint 失败：{}", exc)
    conn.close()
    _local.conn = None
    _local.path = None


def fts5_available() -> bool:
    """当前 SQLite 是否带 FTS5（词法检索的前提）。

    返回：可用为 True。**不可用时词法检索整条链路都不能用**，所以启动自检要看它。
    """
    global _fts5_ok
    if _fts5_ok is None:
        try:
            with closing(sqlite3.connect(":memory:")) as probe:
                probe.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
            _fts5_ok = True
        except sqlite3.OperationalError:
            _fts5_ok = False
    return _fts5_ok
