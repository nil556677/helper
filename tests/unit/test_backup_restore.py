"""T3.3 备份工具的验收：证明「备份 = 复制 data/」是真的，拷回来的库能直接用。

三件事各用一条用例钉住：

1. 做一份备份 → ``--verify`` 一致、``--restore-smoke`` 报告 quick_check=ok 且计数与源一致；
2. 把备份目录**当作新的安装目录**（patch ``project_root``）打开——文档数、片段数、任务数一致，
   quick_check=ok，FTS 能搜到原本能搜到的词（chunks_fts MATCH）；
3. verify 能抓出"文件被篡改"（大小/哈希不符返回不一致）。
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

from app.core.db.connection import close_thread_connection, get_connection
from app.core.db.schema import init_db

# scripts/ 不是包，用 importlib 按文件路径加载，这样测试能直接调用 backup/verify/restore_smoke。
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_BACKUP_MODULE = None

#: 预期计数（seed 造的数据）。
EXPECT_DOCS = 3
EXPECT_CHUNKS = 3
EXPECT_JOBS = 2

#: FTS 应能搜到的原词（unicode61 是按词切分的整词，见 test_db_schema 的单测做法）。
FTS_TERM = "违约金"


def _load_backup_module():
    global _BACKUP_MODULE
    if _BACKUP_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "helper_backup_tool", SCRIPTS_DIR / "backup_data.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        _BACKUP_MODULE = module
    return _BACKUP_MODULE


BACKUP = _load_backup_module()


@pytest.fixture
def seeded(fake_root: Path) -> None:
    """建好 schema 的库 + files/ 副本 + userdict，并塞入 documents/chunks/jobs。"""
    from app.core import paths

    paths.ensure_dirs()
    init_db()
    conn = get_connection()
    now = time.time()

    # 资料副本 + userdict（backup 要一起带走的小文件）。
    sub = paths.files_dir() / "abc123"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "合同.md").write_text("合同 违约金 条款", encoding="utf-8")
    (paths.files_dir() / "note.txt").write_text("备份工具 note", encoding="utf-8")
    (paths.data_dir() / "userdict.txt").write_text("自造词\n", encoding="utf-8")

    for source, stored, size in (
        ("合同.md", "files/abc123/合同.md", 1024),
        ("note.txt", "files/note.txt", 512),
        ("orphan.md", "files/orphan.md", 0),
    ):
        conn.execute(
            "INSERT INTO documents(source_name, stored_path, size, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'ready', ?, ?)",
            (source, stored, size, now, now),
        )
    doc_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM documents ORDER BY id").fetchall()]

    # doc0 两段（第一段带 FTS 可搜词），doc1 一段；共 3 段。
    for seq, (text, tokens) in enumerate(
        (("合同约定违约金", "合同 约定 违约金"), ("第二条 付款", "第二 付款"))
    ):
        conn.execute(
            "INSERT INTO chunks(doc_id, seq, text, tokens, created_at) VALUES (?, ?, ?, ?, ?)",
            (doc_ids[0], seq, text, tokens, now),
        )
    conn.execute(
        "INSERT INTO chunks(doc_id, seq, text, tokens, created_at) VALUES (?, 0, ?, ?, ?)",
        (doc_ids[1], "单独片段", "单独 片段", now),
    )

    for kind in ("import", "cleanup"):
        conn.execute(
            "INSERT INTO jobs(kind, payload, status, next_run_at, created_at, updated_at)"
            " VALUES (?, '{}', 'done', ?, ?, ?)",
            (kind, now, now, now),
        )

    close_thread_connection()


def test_backup_verifies_and_smokes(seeded: None, tmp_path: Path) -> None:
    """备份后：--verify 全一致；--restore-smoke 报告 quick_check=ok 且计数与 seed 一致。"""
    dest = tmp_path / "backup-a"
    manifest = BACKUP.backup(dest)

    assert (dest / "manifest.json").is_file()
    assert manifest["entries"], "备份目录不该是空的"

    ok, problems = BACKUP.verify(dest)
    assert ok, f"备份校验不通过：{problems}"

    smoke = BACKUP.restore_smoke(dest)
    assert smoke["quick_check"] == "ok"
    assert smoke["documents"] == EXPECT_DOCS
    assert smoke["chunks"] == EXPECT_CHUNKS
    assert smoke["jobs"] == EXPECT_JOBS


def test_backup_opens_as_new_project_root(seeded: None, fake_root: Path, tmp_path: Path, monkeypatch) -> None:
    """把备份目录当作新的安装目录打开：计数一致、quick_check=ok、FTS 能搜到原词。"""
    from app.core import paths

    dest = tmp_path / "backup-c"
    BACKUP.backup(dest)

    monkeypatch.setattr(paths, "project_root", lambda: dest)
    init_db()  # 幂等：对备份里的 data/helper.db 打开/确保版本

    conn = get_connection()
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        assert quick == "ok"

        assert int(conn.execute("SELECT count(*) FROM documents").fetchone()[0]) == EXPECT_DOCS
        assert int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0]) == EXPECT_CHUNKS
        assert int(conn.execute("SELECT count(*) FROM jobs").fetchone()[0]) == EXPECT_JOBS

        hits = int(
            conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (FTS_TERM,))
            .fetchone()[0]
        )
        assert hits == 1, "备份库的 FTS 应仍能搜到原资料的说法（违约金）"
    finally:
        close_thread_connection()


def test_verify_detects_tampered_file(seeded: None, tmp_path: Path) -> None:
    """verify 重算 sha256，改动过的文件要被抓出来。"""
    dest = tmp_path / "backup-b"
    BACKUP.backup(dest)

    (dest / "data" / "files" / "note.txt").write_text("tampered", encoding="utf-8")

    ok, problems = BACKUP.verify(dest)
    assert not ok
    assert any("note.txt" in p for p in problems), problems
