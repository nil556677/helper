"""清理任务（cleanup）：孤儿副本、inbox 残留、WAL、历史。

**重点是三条安全线**（它们错了会删掉用户正在用的东西）：

1. 正在导入的副本不能被删（"副本已落盘、资料行还没提交"这段时间库里查不到它）；
2. 被任务引用的暂存文件不能被删（那条任务马上就要读它）；
3. WAL 合并失败不能让整个清理失败。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import tasks as tasks_app
from app.services import knowledge
from app.services import maintenance as maintenance_service
from app.core import paths
from app.core.jobs import api, registry
from app.core.jobs.runner import Runner

SAMPLE = "# 设备采购合同\n\n第一条 乙方应在 30 日内交付设备。\n\n第二条 违约金按日万分之五计算。\n"


@pytest.fixture
def library_db(fake_root: Path) -> Iterator[None]:
    """建好 schema 的空库 + 已注册的 kind。"""
    from app.core.db import schema
    from app.core.db.connection import close_thread_connection

    paths.ensure_dirs()
    schema.init_db()
    registry.clear()
    tasks_app.register_kinds()
    try:
        yield
    finally:
        registry.clear()
        close_thread_connection()


def _age(path: Path, *, seconds: float) -> None:
    """把文件/目录的修改时间调到过去（模拟"放了很久"）。"""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _import_one() -> Path:
    """导一份资料，返回它留下的副本目录（这是"被引用"的那一个）。"""
    data = SAMPLE.encode()
    payload = knowledge.stage_upload("合同.md", data)
    tasks_app.enqueue_import(payload, dedupe_key=knowledge.upload_dedupe_key(data))
    Runner(inline=True).drain()
    return next(path for path in paths.files_dir().iterdir() if path.is_dir())


def test_orphan_copy_removed_but_referenced_kept(library_db: None) -> None:
    """没人引用的副本（且放久了）删掉；被资料引用的副本一个都不许动。"""
    referenced = _import_one()
    orphan = paths.files_dir() / "deadbeef0000"
    orphan.mkdir(parents=True)
    (orphan / "x.md").write_text("x")
    _age(orphan, seconds=1000)

    assert maintenance_service.remove_orphan_copies() == 1
    assert referenced.is_dir(), "被引用的副本被误删了"
    assert not orphan.exists()


def test_fresh_orphan_is_left_alone(library_db: None) -> None:
    """**刚写进来的**孤儿不碰：那很可能是一次正在进行中的导入。"""
    orphan = paths.files_dir() / "fresh0000"
    orphan.mkdir(parents=True)
    (orphan / "x.md").write_text("x")

    assert maintenance_service.remove_orphan_copies() == 0
    assert orphan.is_dir(), "十分钟内动过的副本不该被删"


def test_inbox_leftover_removed_but_referenced_kept(library_db: None) -> None:
    """inbox 里被任务引用的文件不能删（任务马上要读它）；没人引用又放久了的才删。"""
    data = SAMPLE.encode()
    payload = knowledge.stage_upload("待导入.md", data)
    tasks_app.enqueue_import(payload, dedupe_key=knowledge.upload_dedupe_key(data))  # 排队中，还没跑
    referenced = Path(payload["path"])
    assert referenced.is_file()

    old_ghost = paths.inbox_dir() / "old-ghost.md"
    old_ghost.write_text("残留")
    _age(old_ghost, seconds=7200)
    fresh_ghost = paths.inbox_dir() / "fresh-ghost.md"
    fresh_ghost.write_text("刚写进来")

    assert maintenance_service.remove_inbox_leftovers() == 1
    assert referenced.is_file(), "排队任务要用的暂存文件被误删了"
    assert fresh_ghost.is_file(), "一小时内的不该删"
    assert not old_ghost.exists()


def test_cleanup_runs_through_queue_and_reports(library_db: None) -> None:
    """清理是队列里的一条**维护类**任务：跑完把结果写在 note 里（界面行内就能看）。"""
    _import_one()
    orphan = paths.files_dir() / "deadbeef1111"
    orphan.mkdir(parents=True)
    _age(orphan, seconds=1000)

    job_id = tasks_app.enqueue_cleanup("test")
    assert Runner(inline=True).drain() == 1

    job = api.get(job_id)
    assert job is not None and job.status == "done", job
    assert "孤儿副本 1 个" in job.note
    assert "WAL 已合并" in job.note
    assert not orphan.exists()


def test_cleanup_is_deduped(library_db: None) -> None:
    """同一时刻只留一条清理任务（否则频繁开关窗口会把队列塞满维护任务）。"""
    first = tasks_app.enqueue_cleanup("startup")
    assert tasks_app.enqueue_cleanup("manual") == first
    assert len(api.list_jobs(statuses=("pending",))) == 1


def test_wal_checkpoint_failure_does_not_break_cleanup(library_db: None, monkeypatch) -> None:
    """WAL 合并失败只降级成"跳过"，不该让整个清理失败。"""
    import sqlite3

    class Broken:
        def execute(self, *_args: object) -> None:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(maintenance_service, "get_connection", lambda *_a, **_k: Broken())
    assert maintenance_service.checkpoint_wal() is False
