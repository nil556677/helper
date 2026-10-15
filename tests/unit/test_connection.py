"""连接层：生产启动路径（`init_db`）、事务回滚、换库、退出前的 WAL 归并。

为什么值得单独测：`core/db` 是全项目数据一致性的地基，而以前的用例都用
``connect(临时文件)`` 自建连接，**绕过了生产路径**（`init_db()` / `get_connection()`），
所以"路径不对""事务没回滚""连接没关"这些问题测不出来。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core import paths
from app.core.db import schema
from app.core.db.connection import close_thread_connection, get_connection, transaction


def test_init_db_creates_database_at_paths_location(fake_root: Path) -> None:
    """生产入口 `init_db()` 必须在 `paths.db_file()` 上建库（而不是别处）。"""
    version = schema.init_db()

    assert version == schema.SCHEMA_VERSION
    assert paths.db_file().exists()


def test_transaction_rolls_back_on_error(fake_root: Path) -> None:
    """事务里抛异常 → 整段回滚（半截写入是最难查的一类问题）。"""
    schema.init_db()

    with pytest.raises(RuntimeError), transaction() as conn:
        conn.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at)"
            " VALUES ('c1', '', 0, 0)"
        )
        raise RuntimeError("故意的失败")

    count = get_connection().execute("SELECT count(*) AS n FROM conversations").fetchone()["n"]
    assert int(count) == 0


def test_get_connection_switches_database(fake_root: Path, tmp_path: Path) -> None:
    """换库文件时旧连接必须被关掉，否则两条连接会指向不同的库。"""
    first = get_connection(tmp_path / "a.db")
    second = get_connection(tmp_path / "b.db")

    assert first is not second
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")  # 已关闭的连接不能再用

    close_thread_connection()


def test_close_merges_wal_back_into_main_file(fake_root: Path) -> None:
    """退出前 checkpoint：主库文件要是完整的。

    为什么值得钉住：方案说"备份 = 复制 data/ 目录"，而 WAL 模式下最近的写入可能只在
    ``helper.db-wal`` 里——`checkpoint(TRUNCATE)` 之后 ``-wal`` 会被清空。
    """
    schema.init_db()
    get_connection().execute(
        "INSERT INTO conversations(id, title, created_at, updated_at) VALUES ('c1', '', 0, 0)"
    )

    close_thread_connection(checkpoint_first=True)

    wal = paths.db_file().with_name(paths.db_file().name + "-wal")
    assert (not wal.exists()) or wal.stat().st_size == 0
