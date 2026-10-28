"""净室 v1 schema：初始化幂等、版本读取、不兼容拒绝。"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.db import schema_v1
from app.core.db.connection import connect


@pytest.fixture(autouse=True)
def _init(fake_root):
    yield


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row["name"] for row in rows}


def test_initialize_creates_all_v1_tables(fake_root):
    path = fake_root / "data" / "helper.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        assert schema_v1.initialize(conn) == 1
        names = _table_names(conn)
        for expected in (
            "schema_meta",
            "settings",
            "llm_slots",
            "documents",
            "chunks",
            "sparse_terms",
            "sparse_meta",
            "conversations",
            "messages",
            "index_meta",
            "token_dict",
            "jobs",
            "job_events",
            "trace_runs",
            "trace_spans",
            "trace_hits",
            "trace_events",
            "turn_commits",
            "conversation_summaries",
        ):
            assert expected in names, f"缺少表 {expected}"
        job_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        assert "run_id" in job_columns, "jobs 必须有 run_id（任务批次重排依赖它）"
    finally:
        conn.close()


def test_initialize_is_idempotent(fake_root):
    conn = connect()
    try:
        assert schema_v1.initialize(conn) == 1
        assert schema_v1.initialize(conn) == 1
        assert schema_v1.read_version(conn) == 1
    finally:
        conn.close()


def test_initialize_rejects_other_version(fake_root):
    conn = connect()
    try:
        conn.executescript("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);")
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '20')"
        )
        conn.commit()
        with pytest.raises(ValueError, match="不兼容"):
            schema_v1.initialize(conn)
    finally:
        conn.close()


def test_read_version_empty_db_is_zero(fake_root):
    conn = connect()
    try:
        assert schema_v1.read_version(conn) == 0
    finally:
        conn.close()


def test_initialize_adds_missing_v1_tables_on_old_db(fake_root):
    """旧 v1 库缺 turn_commits / conversation_summaries 时，initialize 必须补齐。

    回归：两个"补表"函数的 SQL 把 ``--`` 注释和 CREATE 拼在同一行，SQLite 会把
    CREATE 当注释吞掉 → 建表静默失败，直到运行期才报 ``no such table: turn_commits``。
    """
    conn = connect()
    try:
        assert schema_v1.initialize(conn) == 1
        # 模拟"更早版本的 v1 库"：把后加的两张表删掉（版本号仍是 1）
        conn.executescript(
            "DROP TABLE IF EXISTS turn_commits;"
            "DROP TABLE IF EXISTS conversation_summaries;"
        )
        assert schema_v1.initialize(conn) == 1  # 幂等补表
        names = _table_names(conn)
        assert "turn_commits" in names, "turn_commits 必须被补上（否则落库报 no such table）"
        assert "conversation_summaries" in names, "conversation_summaries 必须被补上"
    finally:
        conn.close()
