"""认领 / 续租 / 释放 / 崩溃恢复：泳道认领、启动恢复、租约回收。"""

from __future__ import annotations

import time
from typing import Any

from app.core.db.connection import get_connection, transaction
from app.core.jobs.model import CANCELLED, FAILED, PENDING, RUNNING, Job
from app.core.jobs.store._shared import CRASH_LIMIT, _insert_event


def claim_next(kinds: tuple[str, ...], *, lease_s: float, worker: str = "", now: float | None = None) -> Job | None:
    """认领一条该泳道的任务（**一条 SQL，天然互斥**）。

    参数：
        kinds: 该泳道允许认领的 kind。
        lease_s: 租约时长（取该泳道里最长的那个，安全侧）。
        worker: 认领者标识（线程名等，排障用）。
        now: 时间戳。
    返回：认领到的任务；没有可跑的就返回 None。
    """
    if not kinds:
        return None
    stamp = time.time() if now is None else now
    placeholders = ",".join("?" for _ in kinds)
    sql = f"""
        UPDATE jobs
           SET status = ?, lease_until = ?, worker = ?, started_at = coalesce(started_at, ?),
               runs = runs + 1, updated_at = ?
         WHERE id = (
             SELECT id FROM jobs
              WHERE status = ? AND cancel_requested = 0 AND next_run_at <= ?
                AND kind IN ({placeholders})
                AND (resource IS NULL
                     OR resource NOT IN (SELECT resource FROM jobs
                                           WHERE status = ? AND resource IS NOT NULL))
              ORDER BY priority DESC, next_run_at
              LIMIT 1)
        RETURNING *
    """
    params = (RUNNING, stamp + float(lease_s), worker, stamp, stamp, PENDING, stamp, *kinds, RUNNING)
    row = get_connection().execute(sql, params).fetchone()
    return Job.from_row(row) if row else None


def _recover(kinds_condition: str, params: tuple[Any, ...], *, now: float | None) -> dict[str, int]:
    """崩溃恢复的公共实现：先收尾"取消中"的，再判"反复崩溃"，最后把剩下的放回 pending。

    每条都记一笔事件（时间线里能看出这次恢复到底把任务怎么了）。
    """
    stamp = time.time() if now is None else now
    with transaction() as conn:
        affected = conn.execute(
            f"SELECT id, runs, fails, cancel_requested FROM jobs WHERE {kinds_condition}", params
        ).fetchall()
        # ① 正在取消的任务：用户已经明确不要了，别再放回队列重跑——直接收尾成 cancelled。
        #    （旧实现把它放回 pending 并复位 cancel_requested=0，于是它会被重新认领并跑完，
        #      等于无视用户的取消。）
        cancelled = conn.execute(
            "UPDATE jobs SET status = ?, note = '已取消（退出时正在取消）', lease_until = NULL,"
            " worker = NULL, finished_at = ?, updated_at = ?"
            f" WHERE {kinds_condition} AND cancel_requested = 1",
            (CANCELLED, stamp, stamp, *params),
        ).rowcount
        # ② 反复崩溃：判死（这些行已变成 failed，下面的 UPDATE 不会再匹配到它们）
        failed = conn.execute(
            "UPDATE jobs SET status = ?, error = '反复崩溃（已认领 ' || runs || ' 次），不再重跑',"
            " error_kind = 'unknown', lease_until = NULL, worker = NULL, finished_at = ?, updated_at = ?"
            f" WHERE {kinds_condition} AND runs - fails >= ?",
            (FAILED, stamp, stamp, *params, CRASH_LIMIT),
        ).rowcount
        # ③ 其余：放回 pending（复位取消标志，保持状态机自洽）
        recovered = conn.execute(
            "UPDATE jobs SET status = ?, note = '上次被中断，正在重跑', lease_until = NULL,"
            " worker = NULL, started_at = NULL, cancel_requested = 0, next_run_at = ?, updated_at = ?"
            f" WHERE {kinds_condition}",
            (PENDING, stamp, stamp, *params),
        ).rowcount
        for row in affected:
            job_id = int(row["id"])
            if int(row["cancel_requested"] or 0):
                _insert_event(
                    conn,
                    job_id,
                    kind="cancelled",
                    message="退出时正在取消，已直接收尾",
                    detail="用户在进程退出前请求了取消，不再放回队列",
                    attempt=int(row["fails"] or 0),
                    now=stamp,
                )
                continue
            crashed_hard = int(row["runs"] or 0) - int(row["fails"] or 0) >= CRASH_LIMIT
            _insert_event(
                conn,
                job_id,
                kind="crash",
                message="反复崩溃，不再重跑" if crashed_hard else "上次进程中断，已放回队列",
                detail="进程被强杀或断电时任务还在跑（租约到期/启动恢复时被回收）",
                attempt=int(row["fails"] or 0),
                now=stamp,
            )
    return {
        "recovered": int(recovered or 0),
        "failed": int(failed or 0),
        "cancelled": int(cancelled or 0),
    }


def recover_on_start(*, now: float | None = None) -> dict[str, int]:
    """启动时恢复：**无条件**把所有 ``running`` 收回 ``pending``。

    为什么不等租约过期：单进程应用，上次进程已死是**确定的事实**——等 60 秒只会让用户
    对着不动的任务页发呆。租约留给运行期的"线程卡死"。
    """
    return _recover(f"status = '{RUNNING}'", (), now=now)


def reclaim_expired(
    *, now: float | None = None, alive: tuple[int, ...] = ()
) -> dict[str, int]:
    """运行期回收：租约过期的 ``running``（线程卡死/被杀）放回 ``pending``。

    ``alive``：**本进程里线程还活着**的任务 id。这些即使租约过期也不回收——
    线程活着说明它可能正卡在一段没有 ``report()`` 的长操作里（大 PDF 解析、全量重建），
    这时把任务改回 pending 会让另一轮再认领它，**同一个任务被跑两遍**。
    真正的"线程卡死"会在下一次心跳停止续租后才进入回收。
    """
    stamp = time.time() if now is None else now
    clause = f"status = '{RUNNING}' AND lease_until IS NOT NULL AND lease_until < ?"
    params: tuple[Any, ...] = (stamp,)
    if alive:
        placeholders = ",".join("?" for _ in alive)
        clause += f" AND id NOT IN ({placeholders})"
        params += tuple(int(job_id) for job_id in alive)
    return _recover(clause, params, now=now)


def renew_lease(
    job_id: int, *, lease_s: float, worker: str | None = None, now: float | None = None
) -> bool:
    """给在跑的任务续租（心跳线程调；不依赖 handler 是否调 ``report()``）。

    为什么需要独立心跳：有些 handler 一步就要跑几分钟（解析大 PDF、全量重建），期间
    没有批次边界可调 ``report()``——只靠 handler 续租，租约必然过期，任务会被误判
    "卡死"回收，然后**双跑**。心跳只看"线程还活着"，与 handler 的进度粒度解耦。

    返回：是否真的续上了（任务已结束或已被回收时返回 False）。
    """
    stamp = time.time() if now is None else now
    sql = (
        "UPDATE jobs SET lease_until = ?, updated_at = ?"
        f" WHERE id = ? AND status = '{RUNNING}'"
    )
    params: tuple[object, ...] = (stamp + float(lease_s), stamp, int(job_id))
    if worker is not None:
        sql += " AND worker = ?"
        params += (worker,)
    with transaction() as conn:
        return bool(conn.execute(sql, params).rowcount)
