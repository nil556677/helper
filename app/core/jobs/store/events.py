"""任务事件与进度：读取事件时间线、写进度并续租、记录成功结果详情。"""

from __future__ import annotations

import time

from app.core.db.connection import get_connection, transaction
from app.core.jobs.model import RUNNING, JobEvent
from app.core.jobs.store._shared import _insert_event


def events(job_id: int, *, limit: int = 50) -> list[JobEvent]:
    """某个任务的事件时间线（新的在后；界面按顺序读）。"""
    rows = get_connection().execute(
        "SELECT * FROM job_events WHERE job_id = ? ORDER BY id LIMIT ?", (job_id, max(1, int(limit)))
    ).fetchall()
    return [JobEvent.from_row(row) for row in rows]


def write_progress(
    job_id: int,
    done: int,
    total: int,
    note: str,
    *,
    lease_s: float,
    worker: str | None = None,
    now: float | None = None,
) -> None:
    """写进度并**续租**（handler 调用 ``report()`` 时走这里）。

    进度按批写、不逐条写：10 万次写库是不可接受的（``JobContext`` 里有 0.2 秒节流）。

    ``worker``：认领者令牌——陈旧 worker 的进度写入会被忽略（那一轮已被回收/重新认领）。
    """
    stamp = time.time() if now is None else now
    progress = 0.0 if not total else min(1.0, max(0.0, done / total))
    sql = (
        "UPDATE jobs SET done = ?, total = ?, progress = ?, note = ?, lease_until = ?, updated_at = ?"
        f" WHERE id = ? AND status = '{RUNNING}'"
    )
    params: tuple[object, ...] = (int(done), int(total), progress, note, stamp + float(lease_s), stamp, job_id)
    if worker is not None:
        sql += " AND worker = ?"
        params += (worker,)
    with transaction() as conn:
        conn.execute(sql, params)


def add_event(
    job_id: int,
    *,
    kind: str,
    message: str,
    detail: str = "",
    attempt: int = 0,
    now: float | None = None,
) -> None:
    """往事件时间线写一笔（诊断/告警用；任务详情里按 kind 展示）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        _insert_event(
            conn, job_id, kind=kind, message=message, detail=detail, attempt=attempt, now=stamp
        )


def record_result(job_id: int, *, detail: str, now: float | None = None) -> None:
    """把一次**成功**任务的结果详情记进事件时间线（界面"详情"里能看到）。

    为什么要它：结果的一句话摘要走 ``jobs.note``（引擎统一写），但"这次到底做了什么"
    往往还有一段更长的话（切了哪些片段、清理了哪些目录）。放进事件表既能展示，又不用改表结构。

    只在 ``detail`` 非空时调用（多数任务只要一句 note，不必每次写一笔事件）。
    """
    if not detail:
        return
    stamp = time.time() if now is None else now
    with transaction() as conn:
        _insert_event(conn, job_id, kind="result", message="执行结果", detail=detail, now=stamp)
