"""数据库分区：SQLite 的连接、表结构与迁移。

对外只暴露 :func:`app.core.db.connection.transaction`（写事务）、
:func:`~app.core.db.connection.get_connection`（线程本地连接）与
:func:`~app.core.db.schema.init_db`（建表，幂等）。
"""

from app.core.db.connection import (
    close_thread_connection,
    connect,
    fts5_available,
    get_connection,
    transaction,
)
from app.core.db.health import (
    last_shutdown_was_clean,
    mark_clean_exit,
    quick_check,
    verify_unclean_exit,
)
from app.core.db.schema import SCHEMA_VERSION, init_db, init_schema, schema_version

__all__ = [
    "SCHEMA_VERSION",
    "close_thread_connection",
    "connect",
    "fts5_available",
    "get_connection",
    "init_db",
    "init_schema",
    "last_shutdown_was_clean",
    "mark_clean_exit",
    "quick_check",
    "schema_version",
    "transaction",
    "verify_unclean_exit",
]
