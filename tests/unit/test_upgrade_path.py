"""T5.2 升级路径测试：老数据目录 → 新程序启动，数据不丢、新表就位、FTS 仍可用。

**为什么需要这条测试**：程序每天启动都会对现有库再跑一次 `init_db`（幂等建表 + 逐条迁移）。
如果哪天 schema 又 +1，老库必须能被这一次启动补成新形状，而且**不能丢任何老数据、不能让
词法索引失效**。这里把"老版本留下的 data 目录"建成一份退路（数据留在 tmp_path 里的库文件），
模拟升级，逐一钉住这些不变式。

**反向用例**：程序永远不能去"降级"运行——若库的版本号比程序还新，说明它是用更新的 helper
建/升的，旧版再往里写只会搞坏它。必须抛 `SchemaTooNewError` 拒绝启动。这条实现已存在，
这里补上测试把它钉住。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app.core.db.connection import get_connection
from app.core.db.schema import (
    SCHEMA_VERSION,
    SchemaTooNewError,
    init_db,
    init_schema,
    schema_version,
    table_names,
)

#: `SCHEMA_SQL` 建的表 + v2/v3 迁移补的表（虚表 FTS5 也在 sqlite_master 的 table 里）。
EXPECTED_TABLES = {
    "schema_meta",
    "documents",
    "chunks",
    "chunks_fts",
    "llm_slots",
    "conversations",
    "messages",
    "settings",
    "index_meta",
    "jobs",
    "job_events",
}


def _build_old_db(db_path: Path) -> None:
    """用**当前** schema 建一份"老数据"：若干文档/片段/任务/设置，留在 tmp_path 里。

    连接按路径缓存在线程本地，后面 re-init 仍复用同一条，数据天然可见。
    """
    assert init_db(db_path) == SCHEMA_VERSION
    conn = get_connection(db_path)
    now = time.time()

    conn.execute(
        "INSERT INTO documents(source_name, stored_path, size, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("合同.pdf", "files/合同.pdf", 1024, "ready", now, now),
    )
    conn.execute(
        "INSERT INTO documents(source_name, stored_path, size, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("说明.txt", "files/说明.txt", 512, "ready", now, now),
    )
    doc_id = int(conn.execute("SELECT MAX(id) AS m FROM documents").fetchone()["m"])

    conn.execute(
        "INSERT INTO chunks(doc_id, seq, text, tokens, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (doc_id, 0, "合同约定违约金为万分之五", "合同 约定 违约 违约金 万分之五", now),
    )
    conn.execute(
        "INSERT INTO chunks(doc_id, seq, text, tokens, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (doc_id, 1, "付款条款见附录。", "付款 条款 附录", now),
    )

    conn.execute(
        "INSERT INTO jobs(kind, payload, status, next_run_at, created_at, updated_at)"
        " VALUES (?, ?, 'pending', ?, ?, ?)",
        ("ingest.file", '{"path": "files/合同.pdf"}', now, now, now),
    )
    conn.execute(
        "INSERT INTO jobs(kind, payload, status, next_run_at, created_at, updated_at)"
        " VALUES (?, ?, 'done', ?, ?, ?)",
        ("dummy", "{}", now, now, now),
    )

    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
        ("theme", '"dark"', now),
    )
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
        ("language", '"zh"', now),
    )
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
        ("first_run", "true", now),
    )


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    """采样各张表的行数，供升级前后比对。"""
    return {
        "documents": int(conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]),
        "chunks": int(conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]),
        "jobs": int(conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]),
        "settings": int(conn.execute("SELECT count(*) AS n FROM settings").fetchone()["n"]),
    }


def _hits(conn: sqlite3.Connection, term: str) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM chunks_fts WHERE chunks_fts MATCH ?", (term,)
    ).fetchone()
    return int(row["n"])


def test_upgrade_preserves_old_data_and_reaches_current_version(
    tmp_path: Path,
) -> None:
    """老库重跑 init_db：版本到位、计数一致、新表/索引就位。

    这正是每天启动时对已有库要做的事（幂等建表 + 补迁移）。
    """
    db_path = tmp_path / "data" / "helper.db"
    _build_old_db(db_path)
    conn = get_connection(db_path)
    before = _counts(conn)

    version = init_db(db_path)

    assert version == SCHEMA_VERSION, "升级后库版本必须=程序版本"
    assert schema_version(conn) == SCHEMA_VERSION
    assert _counts(conn) == before, "老数据一行都不能丢"
    assert table_names(conn) >= EXPECTED_TABLES, "v1..v3 的表应全部就位"
    # v3 迁移补上的列也要在（jobs 表里确实添上了 error_kind）
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "error_kind" in cols, "v3 迁移的 ALTER 应已生效"


def test_upgrade_fts_still_hits_old_and_new(tmp_path: Path) -> None:
    """升级后词法索引不能失效：老片段搜得着，升级后再插入的新片段也要能搜到。"""
    db_path = tmp_path / "data" / "helper.db"
    _build_old_db(db_path)
    conn = get_connection(db_path)

    init_db(db_path)

    assert _hits(conn, "违约") == 1, "老片段升级后必须仍被 FTS 命中"
    assert _hits(conn, "付款") == 1

    # 触发器和 FTS 外部内容表在升级后依然在跑：新插入要能立刻搜到
    conn.execute(
        "INSERT INTO chunks(doc_id, seq, text, tokens, created_at)"
        " VALUES ((SELECT MAX(id) FROM documents), 9, '后续新增条款', '后续 新增 条款', ?)",
        (time.time(),),
    )
    assert _hits(conn, "新增") == 1, "升级后再插入的新片段也应能被 FTS 命中"


def test_init_schema_is_idempotent_after_upgrade(tmp_path: Path) -> None:
    """连跑两遍升级（重复启动）仍然安全：版本不变、不炸、FTS 正常。"""
    db_path = tmp_path / "data" / "helper.db"
    _build_old_db(db_path)
    conn = get_connection(db_path)

    assert init_schema(conn) == SCHEMA_VERSION
    assert init_schema(conn) == SCHEMA_VERSION
    assert schema_version(conn) == SCHEMA_VERSION
    assert _hits(conn, "违约金") == 1


def test_newer_database_refuses_to_start(tmp_path: Path) -> None:
    """反向：库的版本号比程序新 → 拒绝启动并给出明确信息。

    把 schema_meta.schema_version 改成 99，模拟"用更新版建过的库被老程序打开"。
    绝不能静默继续写（那会弄坏不该由老程序碰的库）。
    """
    from app.core.db import schema as schema_module

    db_path = tmp_path / "data" / "helper.db"
    _build_old_db(db_path)
    conn = get_connection(db_path)
    conn.execute("UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'")

    with pytest.raises(SchemaTooNewError) as excinfo:
        schema_module.init_db(db_path)
    message = str(excinfo.value)
    assert "99" in message, "报错里应点明库的真实版本号"
    assert "还新" in message or "比这个程序" in message, "报错应说明是库比程序新"
