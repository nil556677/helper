"""schema v1 的硬约束：表齐全、版本记录、FTS 触发器与级联删除真的生效。

**为什么这些必须被测试钉住**：词法检索完全依赖 ``chunks_fts`` 的触发器同步。
触发器要是漏了（或被人"顺手"删掉），表现是"改了正文搜不到新词、删了资料还搜得到"
——这类问题只有测试能拦住。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app.core.db.connection import connect, fts5_available
from app.core.db.schema import SCHEMA_VERSION, init_schema, schema_version, table_names

#: v1 应当建出来的表（少一张都算 schema 坏了）
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
}


def _fresh(tmp_path: Path) -> sqlite3.Connection:
    """建一个全新的临时库（连接由调用方负责，测试结束即随进程回收）。"""
    conn = connect(tmp_path / "t.db")
    init_schema(conn)
    return conn


def _seed_document(conn: sqlite3.Connection) -> int:
    """塞一份资料，返回它的 id。"""
    now = time.time()
    conn.execute(
        "INSERT INTO documents(source_name, stored_path, size, status, created_at, updated_at)"
        " VALUES ('合同.pdf', 'files/x/合同.pdf', 1024, 'ready', ?, ?)",
        (now, now),
    )
    return int(conn.execute("SELECT id FROM documents").fetchone()["id"])


def test_init_creates_all_tables(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    assert EXPECTED_TABLES <= table_names(conn)
    assert schema_version(conn) == SCHEMA_VERSION


def test_init_is_idempotent(tmp_path: Path) -> None:
    """重复建表不炸、版本不变（启动路径每次都会调它）。"""
    conn = _fresh(tmp_path)
    assert init_schema(conn) == SCHEMA_VERSION
    assert schema_version(conn) == SCHEMA_VERSION


def test_fts5_is_available() -> None:
    """词法检索的前提：这套 SQLite 必须带 FTS5。"""
    assert fts5_available(), "FTS5 不可用则词法检索整条链路都不能用"


def test_tokens_index_tracks_insert_update_delete(tmp_path: Path) -> None:
    """触发器三件套：插入能搜到、改 tokens 后旧词失效、删掉后索引也没了。"""
    conn = _fresh(tmp_path)
    doc_id = _seed_document(conn)
    now = time.time()

    conn.execute(
        "INSERT INTO chunks(doc_id, seq, text, tokens, created_at) VALUES (?, 0, ?, ?, ?)",
        (doc_id, "合同约定违约金为万分之五", "合同 约定 违约 违约金 万分之五", now),
    )
    chunk_id = int(conn.execute("SELECT id FROM chunks").fetchone()["id"])

    def hits(term: str) -> int:
        row = conn.execute(
            "SELECT count(*) AS n FROM chunks_fts WHERE chunks_fts MATCH ?", (term,)
        ).fetchone()
        return int(row["n"])

    assert hits("违约") == 1, "插入后应当搜得到"
    assert hits("合同") == 1

    conn.execute("UPDATE chunks SET tokens = ? WHERE id = ?", ("付款 条款", chunk_id))
    assert hits("违约") == 0, "改过 tokens 之后旧词不能再命中"
    assert hits("付款") == 1

    conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
    assert hits("付款") == 0, "删掉之后索引里也要没"


def test_document_delete_cascades_to_chunks(tmp_path: Path) -> None:
    """删资料要把它的片段一起删（靠外键级联，所以连接必须开 foreign_keys）。"""
    conn = _fresh(tmp_path)
    doc_id = _seed_document(conn)
    conn.execute(
        "INSERT INTO chunks(doc_id, seq, text, tokens, created_at) VALUES (?, 0, 'x', 'x', ?)",
        (doc_id, time.time()),
    )

    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

    assert int(conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]) == 0


def test_messages_seq_unique_per_conversation(tmp_path: Path) -> None:
    """会话内序号唯一：并发写同一序号要被数据库挡住（应用层靠 MAX+1 排队）。"""
    conn = _fresh(tmp_path)
    now = time.time()
    conn.execute(
        "INSERT INTO conversations(id, title, created_at, updated_at) VALUES ('c1', '', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO messages(conversation_id, seq, role, content, created_at)"
        " VALUES ('c1', 0, 'user', '你好', ?)",
        (now,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO messages(conversation_id, seq, role, content, created_at)"
            " VALUES ('c1', 0, 'user', '又来一条', ?)",
            (now,),
        )


def test_missing_migration_script_refuses_to_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`SCHEMA_VERSION` 涨了但 `MIGRATIONS` 没补 → **拒绝启动**。

    为什么这么严：以前是"warning + 继续"，然后照样把版本号写成新的——那会造出一个
    "版本号涨了、表却没改"的库，而且重跑也不会再补，用户的数据就这么废了。
    """
    from app.core.db import schema as schema_module

    conn = _fresh(tmp_path)
    monkeypatch.setattr(schema_module, "SCHEMA_VERSION", schema_module.SCHEMA_VERSION + 1)

    with pytest.raises(RuntimeError, match="缺少 schema"):
        schema_module.init_schema(conn)


def test_newer_database_refuses_to_start(tmp_path: Path) -> None:
    """库的版本比程序新（拿旧程序开新库）→ 拒绝，而不是继续往里写。"""
    from app.core.db import schema as schema_module

    conn = _fresh(tmp_path)
    conn.execute("UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'")

    with pytest.raises(schema_module.SchemaTooNewError):
        schema_module.init_schema(conn)
