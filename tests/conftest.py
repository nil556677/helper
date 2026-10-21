"""测试公共装置（重构后重建的最小版）。

提供：临时目录隔离（fake_root / rag_db / ingest_db / tmp_path），
让测试不碰真实项目目录、不碰真实 Qdrant 数据。
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest


def _safe_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.\-]+", "_", name)[:60] or "case"


def _tests_tmp() -> Path:
    from app.core import paths

    return paths.tmp_dir()


@pytest.fixture(scope="session", autouse=True)
def _sqlite_temp_inside_workspace() -> Iterator[None]:
    from app.core import paths

    tmp = paths.tmp_dir()
    tmp.mkdir(parents=True, exist_ok=True)
    names = ("TEMP", "TMP", "SQLITE_TMPDIR")
    originals = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ[name] = str(tmp)
    try:
        yield
    finally:
        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    base = _tests_tmp() / f"{_safe_name(request.node.name)}-{os.getpid()}"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from app.core import paths
    from app.core.config import access as config_access
    from app.core.db import connection

    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "project_root", lambda: root)
    config_access.invalidate()
    connection.close_thread_connection()
    try:
        yield root
    finally:
        connection.close_thread_connection()
        config_access.invalidate()


@pytest.fixture
def ingest_db(fake_root: Path) -> Iterator[None]:
    from app.core import paths
    from app.core.db import schema
    from app.core.db.connection import close_thread_connection

    paths.ensure_dirs()
    schema.init_db()
    yield
    close_thread_connection()


@pytest.fixture
def rag_db(fake_root: Path) -> Iterator[None]:
    from app.core import paths
    from app.core.db import schema
    from app.core.db.connection import close_thread_connection

    paths.ensure_dirs()
    schema.init_db()
    yield
    close_thread_connection()
