"""reset_workspace：dry-run 不动数据；真实执行保留 llm_slots、清旧库/向量存储。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.db import schema_v1
from app.core.db.connection import connect


@pytest.fixture
def old_world(fake_root: Path) -> dict[str, Path]:
    data = fake_root / "data"
    (data / "files" / "abc").mkdir(parents=True, exist_ok=True)
    (data / "files" / "abc" / "a.txt").write_text("hello", encoding="utf-8")
    (data / "langgraph").mkdir(parents=True, exist_ok=True)
    (data / "qdrant-server" / "collections").mkdir(parents=True, exist_ok=True)
    (data / "qdrant-server" / "aliases").mkdir(parents=True, exist_ok=True)

    db = data / "helper.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE settings(key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE llm_slots(
                slot TEXT PRIMARY KEY, provider TEXT NOT NULL DEFAULT 'openai_compatible',
                base_url TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                temperature REAL NOT NULL DEFAULT 0.2, timeout_s REAL NOT NULL DEFAULT 60,
                max_tokens INTEGER NOT NULL DEFAULT 2048, api_key_cipher BLOB,
                key_tail TEXT, updated_at REAL NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '20')")
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES ('vector.url',"
            " '\"http://127.0.0.1:6333\"', 1)"
        )
        conn.execute(
            "INSERT INTO llm_slots(slot, provider, base_url, model, temperature, timeout_s,"
            " max_tokens, api_key_cipher, key_tail, updated_at) VALUES"
            " ('llm', 'openai_compatible', 'http://v', 'm', 0.2, 60, 2048, X'010203', 'abcd', 1)"
        )
        conn.commit()
    finally:
        conn.close()
    return {"db": db, "data": data}


def _run_reset(*, dry_run: bool, keep_llm_slots: bool = True, reimport: bool = False, importer=None) -> None:
    import importlib

    reset_workspace = importlib.import_module("scripts.reset_workspace")
    code = reset_workspace.run(
        dry_run=dry_run,
        keep_llm_slots=keep_llm_slots,
        reimport=reimport,
        importer=importer,
    )
    assert code == 0


def test_dry_run_does_nothing(old_world):
    _run_reset(dry_run=True)
    assert old_world["db"].exists()
    assert (old_world["data"] / "langgraph").exists()
    assert (old_world["data"] / "files" / "abc" / "a.txt").exists()


def test_real_reset_preserves_llm_slots_and_clears(old_world):
    _run_reset(dry_run=False, keep_llm_slots=True)

    # helper.db 会以 v1 重建（旧库内容已清除）
    assert old_world["db"].exists()
    assert not (old_world["data"] / "langgraph").exists()
    assert not (old_world["data"] / "qdrant-server" / "collections").exists()
    assert not (old_world["data"] / "qdrant-server" / "aliases").exists()
    assert (old_world["data"] / "files" / "abc" / "a.txt").exists()

    backups = list((old_world["data"] / "backup").glob("helper-pre-v2-*.db"))
    assert backups, "应生成备份"

    conn = connect()
    try:
        assert schema_v1.read_version(conn) == 1
        row = conn.execute("SELECT * FROM llm_slots WHERE slot = 'llm'").fetchone()
        assert row is not None
        assert row["base_url"] == "http://v"
        assert row["model"] == "m"
        assert bytes(row["api_key_cipher"]) == b"\x01\x02\x03"
        # 服务端地址保留
        url_row = conn.execute(
            "SELECT value_json FROM settings WHERE key = 'vector.url'"
        ).fetchone()
        assert url_row is not None and "6333" in str(url_row[0])
        # 旧表不存在
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "rag_parents" not in tables
    finally:
        conn.close()


def test_reimport_calls_importer(old_world):
    from types import SimpleNamespace

    calls = []

    def importer(path, name=None):
        calls.append((path.name, name))
        return SimpleNamespace(status="imported", reason="")

    _run_reset(dry_run=False, keep_llm_slots=True, reimport=True, importer=importer)
    assert calls == [("a.txt", "a.txt")]
