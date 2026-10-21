"""批次回放（``run_id``）：已结束的任务能按原参数整批重排。"""

from __future__ import annotations

from app.core.db.connection import get_connection, transaction
from app.core.jobs import store


def test_requeue_run_replays_finished_jobs(rag_db: None) -> None:
    """``run_id`` 相同的已完成任务，重排后变成新的 pending 任务。"""
    del rag_db
    job_id, created = store.enqueue("cleanup", {"reason": "test"}, run_id="run-42")
    assert created
    with transaction() as conn:
        conn.execute("UPDATE jobs SET status = 'done', finished_at = 1.0 WHERE id = ?", (job_id,))

    assert store.requeue_run("run-42") == 1
    rows = get_connection().execute(
        "SELECT status, run_id FROM jobs WHERE run_id = 'run-42' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert str(rows[0]["status"]) == "done"
    assert str(rows[1]["status"]) == "pending"
    assert str(rows[1]["run_id"]) == "run-42"


def test_requeue_run_ignores_running_jobs(rag_db: None) -> None:
    """还在排队/在跑的不重排（避免同一任务跑两遍）。"""
    del rag_db
    store.enqueue("cleanup", {"reason": "a"}, run_id="run-7")
    assert store.requeue_run("run-7") == 0
