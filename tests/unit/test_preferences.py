"""界面偏好（窗口几何）的持久化：存在 SQLite 的 settings 表里。

为什么值得测：这是"用户调过一次、下次要一样"的东西——存坏了或者读坏了，
用户体感就是"我调了半天，它每次都不记得"，而这在界面上没有任何报错可看。
"""

from __future__ import annotations

from pathlib import Path

from app.core.db import schema
from app.services import preferences


def _prepare(fake_root: Path) -> None:
    """在临时安装目录里把库建出来（偏好表属于 schema v1）。"""
    schema.init_db()


def test_roundtrip(fake_root: Path) -> None:
    """存进去再读出来，字节完全一致。"""
    _prepare(fake_root)
    payload = bytes(range(64))

    preferences.save_window_geometry(payload)

    assert preferences.load_window_geometry() == payload


def test_missing_returns_none(fake_root: Path) -> None:
    """没存过：返回 None（调用方据此用默认尺寸）。"""
    _prepare(fake_root)
    assert preferences.load_window_geometry() is None


def test_corrupt_value_returns_none(fake_root: Path) -> None:
    """值被改坏：按"没存过"处理，而不是抛异常把启动流程带崩。"""
    _prepare(fake_root)
    from app.core.db.connection import transaction

    with transaction() as conn:
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES ('window_geometry', ?, 0)",
            ("这不是 JSON",),
        )

    assert preferences.load_window_geometry() is None


def test_overwrite_keeps_latest(fake_root: Path) -> None:
    """再存一次要覆盖（用 ON CONFLICT，不是又插一行）。"""
    _prepare(fake_root)
    preferences.save_window_geometry(b"first")
    preferences.save_window_geometry(b"second")

    assert preferences.load_window_geometry() == b"second"
    from app.core.db.connection import get_connection

    count = get_connection().execute("SELECT count(*) AS n FROM settings").fetchone()["n"]
    assert int(count) == 1
