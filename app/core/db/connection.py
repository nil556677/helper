"""SQLite 连接的唯一入口。

**为什么单独一个模块**：全项目的数据（资料、片段、会话、设置、模型槽位）都在同一个库文件里，
连接参数（WAL / 外键 / busy_timeout / row_factory）必须处处一致，否则会出现
"一半连接开了外键、一半没开"——外键级联删除会在某条路径上静默失效，而这种错查不出来。

**线程模型**：导入与检索会跑在工作线程里，而 sqlite3 连接默认禁止跨线程使用。
这里按线程各持一条连接（``threading.local``），并把事务显式化：连接统一用
``isolation_level=None``（自动提交），要事务就调 :func:`transaction`
——这样 Python 的隐式 BEGIN 不会和我们显式的 ``BEGIN IMMEDIATE`` 打架。

``transaction()`` 支持**嵌套**（进度回调等会在活跃事务里写库）：首层 ``BEGIN IMMEDIATE``
拿写锁，嵌套层降级为 ``SAVEPOINT``，不再报 "cannot start a transaction within a transaction"。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from loguru import logger

from app.core import paths

#: 写锁等待上限：桌面场景下写操作很短，等 5 秒还没拿到锁说明有别的东西出了问题。
DEFAULT_BUSY_TIMEOUT_MS = 5000

#: 线程本地连接（一条连接只在自己的线程里用）。
_local = threading.local()


def _txn_depth(conn_id: int, delta: int = 0) -> int:
    """当前线程为这条连接维护的**事务嵌套深度**。

    ``transaction()`` 首层 ``BEGIN IMMEDIATE``，嵌套层用 ``SAVEPOINT``（savepoint 是 SQLite
    为"事务里再开事务"设计的原生机制）。深度按 ``id(conn)`` 记在线程本地，避免把不同
    连接的深度混在一起，也保证连接被换掉／关掉后状态不会被别条连接读到。
    """
    depths = getattr(_local, "txn_depth", None)
    if depths is None:
        depths = {}
        _local.txn_depth = depths
    if delta:
        depths[conn_id] = depths.get(conn_id, 0) + delta
    return depths.get(conn_id, 0)


def _forget_depth(conn: sqlite3.Connection) -> None:
    """丢掉一条连接的事务深度记录（**关连接时必须调用**）。

    为什么：深度表按 ``id(conn)`` 做键，而 CPython 的对象 id 在对象被回收后**会被复用**。
    旧连接的深度没清掉时，新连接可能恰好拿到同一个 id，于是它的第一层事务被误判成
    "嵌套"→ 走 SAVEPOINT 而不是 BEGIN——那个 savepoint 不存在，直接报错。
    """
    depths = getattr(_local, "txn_depth", None)
    if depths:
        depths.pop(id(conn), None)

#: FTS5 可用性只探测一次（它在进程生命周期里不会变）。
_fts5_ok: bool | None = None


def _configure(conn: sqlite3.Connection, *, readonly: bool = False) -> sqlite3.Connection:
    """设置本项目的连接约定（每次建连都要走这里）。

    readonly=True：供 **只读进程/连接**（UI）用，跳过 ``journal_mode=WAL``（那是一个需要
    写权限的 PRAGMA，只读连接上会失败）；WAL 模式是库级持久设置，worker 已开启，只读方直接读。
    """
    conn.row_factory = sqlite3.Row
    if not readonly:  # 只读连接不设 WAL（会失败）
        # WAL：读写并发，写不挡读；synchronous=NORMAL 在 WAL 下已能保证不损坏，换更快的写。
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # 外键默认是关的，级联删除全靠它。
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
    # 临时表/排序**放内存**：不用系统的 TEMP 目录。DELETE 级联、FTS 合并等操作会用到
    # 临时文件，而"系统 TEMP 不可用/被劫持"时 SQLite 会报 unable to open database file
    # （实测：应用进程里删除资料/重建连续报这个错，而库本身是好的）。
    conn.execute("PRAGMA temp_store=MEMORY")
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
    try:
        conn = sqlite3.connect(str(target), isolation_level=None)
    except sqlite3.Error as exc:
        # 打不开库是**最需要现场**的一类错误：把路径与目录状态一起记下来
        logger.error(
            "打开数据库失败：path={} 存在={} 可写={} 错误={}",
            target,
            target.exists(),
            os.access(str(target.parent), os.W_OK),
            exc,
        )
        raise
    return _configure(conn)


def _usable(conn: sqlite3.Connection) -> bool:
    """缓存连接还能用吗（一句 ``PRAGMA`` 探活，失败就丢弃重开）。

    为什么需要：库文件可能被**外部进程**动过（备份恢复、另一个进程打开后异常退出、
    文件被替换）——此时缓存连接手里的文件句柄已经失效，后续写入会报
    ``unable to open database file``（实测：另一个进程崩溃后，重建任务连续失败）。
    探活失败就重开一条，任务不用重启应用也能继续。
    """
    try:
        conn.execute("PRAGMA schema_version").fetchone()
        return True
    except sqlite3.Error:
        return False


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """取当前线程的连接（没有、或旧连接已失效，就建一条新的）。

    参数：
        db_path: 库文件路径；默认 ``{数据目录}/helper.db``。与缓存中的路径不一致时重开。
    返回：当前线程的连接。
    """
    target = db_path or paths.db_file()
    cached = getattr(_local, "conn", None)
    if cached is not None:
        cached_path = getattr(_local, "path", None)
        if cached_path == target and _usable(cached):
            return cached
        # 换了库文件（测试里常见）或旧连接已失效：关掉旧的再重开。
        with contextlib.suppress(sqlite3.Error):
            cached.close()
        _forget_depth(cached)  # id() 会被新连接复用：旧深度必须清掉
        _local.conn = None
        _local.path = None
    conn = connect(target)
    _local.conn = conn
    _local.path = target
    return conn


@contextmanager
def transaction(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """写事务：``BEGIN IMMEDIATE`` → 交还连接 → 提交；出异常回滚。

    **可重入（用 savepoint）**：若当前线程的连接已经在事务里（导入流式解析时进度回调
    会在线程的活跃事务里写库），就不再 ``BEGIN``——那会报
    ``cannot start a transaction within a transaction``——而是发 ``SAVEPOINT``：
    内层 ``commit`` 变 ``RELEASE SAVEPOINT``、内层 ``rollback`` 变 ``ROLLBACK TO SAVEPOINT``。
    内层回滚不会影响外层事务里已提交的部分，外层提交才真正落盘。

    为什么用 ``BEGIN IMMEDIATE``：先把写锁拿到手。若等提交时才发现锁冲突，
    SQLite 只能报 SQLITE_BUSY，而且此时已经执行了一半语句。

    参数：
        db_path: 库文件路径；默认当前线程的连接。
    异常：
        原样抛出块内的异常（回滚后）。
    """
    conn = get_connection(db_path)
    conn_id = id(conn)
    depth = _txn_depth(conn_id, +1)
    sp: str | None = None
    try:
        if depth == 1:
            # 真正的首层事务：拿写锁。
            conn.execute("BEGIN IMMEDIATE")
        else:
            # 已经在一个事务里：开 savepoint 而不是再 BEGIN。
            sp = f"sp_{conn_id}_{depth}"
            conn.execute(f"SAVEPOINT {sp}")
    except BaseException:
        # BEGIN/SAVEPOINT 本身失败（如 busy）：不欠深度，直接抛。
        _txn_depth(conn_id, -1)
        raise
    try:
        yield conn
    except BaseException:
        if sp is None:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
        raise
    else:
        if sp is None:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {sp}")
    finally:
        _txn_depth(conn_id, -1)


def wal_checkpoint(conn: sqlite3.Connection, *, retries: int = 3, delay_s: float = 0.1) -> bool:
    """把 WAL 合并回主库，**并确认它真的完成了**。

    为什么不能只看"执行没抛异常"：``PRAGMA wal_checkpoint(TRUNCATE)`` 在**有其它连接**
    正在读/写时返回 ``busy=1``，此时它什么都没做——但调用方若直接返回 True，就会对外
    宣称"WAL 已合并"，于是"只拷 helper.db"的备份丢掉最近的写入（实测踩过）。

    返回：True = 已合并（主库文件完整）；False = 仍然 busy 或执行失败（调用方应告警，
    不要宣称已合并）。
    """
    for attempt in range(max(1, int(retries))):
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error as exc:
            logger.warning("WAL checkpoint 执行失败：{}", exc)
            return False
        # PRAGMA 返回 (busy, log, checkpointed)：busy=0 才表示全部合并完
        busy = int(row[0]) if row is not None else 0
        if busy == 0:
            return True
        if attempt + 1 < max(1, int(retries)):
            time.sleep(delay_s)
    logger.warning("WAL checkpoint 多次尝试后仍然 busy（有其它连接在读/写，主库可能不完整）")
    return False


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
    if checkpoint_first and not wal_checkpoint(conn):
        logger.warning("退出时 WAL 没能完整合并——若只备份 helper.db，最近写入可能缺失")
    conn.close()
    _forget_depth(conn)  # id() 会被新连接复用：旧深度必须清掉
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
