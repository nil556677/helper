"""T3.2 数据库体检：干净标记 / 非干净退出时体检 / 体检发现问题。

用 fake_root 建一个临时库，直接测 db/health.py 的逻辑，不真跑 bootstrap。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.db import health
from app.core.db import schema as db_schema
from app.core.db.health import (
    last_shutdown_was_clean,
    mark_clean_exit,
    verify_unclean_exit,
)


@pytest.fixture
def fresh_db(fake_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """建库（health 需要 settings 表）。"""
    db_schema.init_db()


def test_mark_then_clean(fresh_db) -> None:
    """先 mark_clean_exit，则 last_shutdown_was_clean 为 True 且会清掉标记。"""
    mark_clean_exit()
    assert last_shutdown_was_clean() is True
    # 读一次就被清掉：再读就是 False（本轮进行中）
    assert last_shutdown_was_clean() is False


def test_no_mark_is_unclean(fresh_db) -> None:
    """从没干净退出过 → 判为非干净。"""
    assert last_shutdown_was_clean() is False


def test_verify_skips_when_clean(fresh_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """上次干净退出 → verify_unclean_exit 不做体检、返回 None。"""
    mark_clean_exit()
    calls: list[str] = []

    def fake_check() -> str:
        calls.append("checked")
        return "ok"

    monkeypatch.setattr(health, "quick_check", fake_check)
    assert verify_unclean_exit() is None
    assert calls == [], "干净退出不该触发体检"


def test_verify_runs_when_unclean(fresh_db) -> None:
    """上次没干净退出 → 做体检，healthy → 返回 None。"""
    assert health.quick_check() == "ok"
    assert verify_unclean_exit() is None  # fresh 库是干净的


def test_verify_reports_problem(fresh_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """体检发现损坏 → 返回问题原文。"""
    def fake_check() -> str:
        return "database disk image is malformed"

    monkeypatch.setattr(health, "quick_check", fake_check)
    result = verify_unclean_exit()
    assert result is not None and "malformed" in result
