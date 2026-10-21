"""任务行 CRUD 与状态迁移：create/read/list/update、取消、标记、计数、清理、暂停。"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from app.core.db.connection import get_connection, transaction
from app.core.jobs.model import (
    CANCELLED,
    DONE,
    FAILED,
    FINISHED_STATUSES,
    PENDING,
    RUNNING,
    Job,
)
from app.core.jobs.store._shared import (
    _STATUS_ORDER,
    BACKOFF_BASE_S,
    BACKOFF_MAX_S,
    KEEP_FINISHED_DAYS,
    KEEP_FINISHED_ROWS,
    PAUSED_KEY,
    _insert_event,
)


def _owned_clause(
    assignments: str, params: tuple[Any, ...], job_id: int, worker: str | None
) -> tuple[str, tuple[Any, ...]]:
    """构造「改这些字段 + 按 id 更新（给了 worker 令牌就再校验归属）」的 SQL。

    **为什么要 worker 令牌**：任务可能因租约到期被回收、随后被另一轮认领。旧线程若在
    事后写进度/收尾，不校验归属就会覆盖新一轮的状态（实测会导致「进度倒退 / 已取消又变完成」）。
    ``worker=None`` 表示调用方没有归属概念（测试、维护动作），行为与以前一致。
    """
    sql = f"{assignments} WHERE id = ?"
    values: tuple[Any, ...] = (*params, int(job_id))
    if worker is not None:
        sql += " AND worker = ?"
        values += (worker,)
    return sql, values


def set_cursor(
    job_id: int, value: str, *, worker: str | None = None, now: float | None = None
) -> None:
    """记下断点续跑位置（立刻落库：它就是为"崩了以后接着跑"准备的）。

    ``worker``：认领者令牌。传了就**只在该任务仍由这一轮 worker 持有时**才写——
    否则旧线程的收尾会把已经被回收/重新认领的新一轮 cursor 覆盖掉。
    """
    stamp = time.time() if now is None else now
    clause, params = _owned_clause("cursor = ?, updated_at = ?", (value, stamp), job_id, worker)
    with transaction() as conn:
        conn.execute(f"UPDATE jobs SET {clause}", params)


def enqueue(
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    priority: int = 0,
    delay_s: float = 0.0,
    max_attempts: int = 3,
    payload_version: int = 1,
    resource: str | None = None,
    run_id: str | None = None,
    now: float | None = None,
) -> tuple[int, bool]:
    """插入一条任务。

    参数：
        kind: 任务类型（由调用方保证已注册）。
        payload: 任务参数（**只放引用**：路径、id；绝不能放文件字节）。
        dedupe_key: 活跃期去重键；命中的话返回已有任务，不新建。
        priority: 优先级（越大越先跑）。
        delay_s: 延后多久才允许被认领。
        max_attempts: 业务失败重试上限（**快照进这一行**：以后改 kind 的默认值不影响已排的任务）。
        payload_version: payload 结构版本。
        resource: 独占资源名（同名资源同时只允许一个任务在跑）。
        now: 时间戳（测试传固定值）。
    返回：``(任务 id, 是否新建)``。
    """
    stamp = time.time() if now is None else now
    # **先查活跃任务再插入**：把"唯一索引冲突"从常规控制流里拿掉——冲突后再查，
    # 若那条活跃任务恰好在两次之间结束，就会把原始 IntegrityError 抛给调用方（TOCTOU）。
    if dedupe_key:
        existing = active_id_for(dedupe_key, kind)
        if existing is not None:
            return existing, False
    values = (
        kind,
        json.dumps(payload or {}, ensure_ascii=False),
        int(payload_version),
        dedupe_key,
        PENDING,
        int(priority),
        stamp + max(0.0, float(delay_s)),
        max(1, int(max_attempts)),
        resource,
        run_id,
        stamp,
        stamp,
    )
    try:
        with transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(kind, payload, payload_version, dedupe_key, status, priority,"
                " next_run_at, max_attempts, resource, run_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            return int(cursor.lastrowid or 0), True
    except sqlite3.IntegrityError:
        # 并发下仍可能撞唯一索引（两个线程同时通过上面的预查）：再查一次，命中就复用。
        existing = active_id_for(dedupe_key, kind) if dedupe_key else None
        if existing is None:
            raise
        return existing, False


def requeue_run(run_id: str) -> int:
    """把某批（``run_id``）里**已结束**的任务按原参数重新入队，返回新入队条数。

    只重排 ``done`` / ``failed`` / ``cancelled``：还在排队或在跑的不动（避免跑两遍）。
    """
    if not run_id:
        return 0
    rows = get_connection().execute(
        "SELECT kind, payload, payload_version, dedupe_key, priority, max_attempts, resource"
        " FROM jobs WHERE run_id = ? AND status IN ('done', 'failed', 'cancelled')"
        " ORDER BY id",
        (run_id,),
    ).fetchall()
    count = 0
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        _, created = enqueue(
            str(row["kind"]),
            payload,
            dedupe_key=row["dedupe_key"],
            priority=int(row["priority"] or 0),
            max_attempts=int(row["max_attempts"] or 3),
            payload_version=int(row["payload_version"] or 1),
            resource=row["resource"],
            run_id=run_id,
        )
        if created:
            count += 1
    return count


def active_id_for(dedupe_key: str, kind: str) -> int | None:
    """找一条活跃（排队或在跑）的同 dedupe 任务。"""
    row = get_connection().execute(
        f"SELECT id FROM jobs WHERE kind = ? AND dedupe_key = ? AND status IN"
        f" ('{PENDING}', '{RUNNING}') ORDER BY id LIMIT 1",
        (kind, dedupe_key),
    ).fetchone()
    return int(row["id"]) if row else None


def get_job(job_id: int) -> Job | None:
    """按 id 取任务。"""
    row = get_connection().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def finish(job_id: int, *, note: str = "", worker: str | None = None, now: float | None = None) -> None:
    """标记成功（``progress`` 直接满格，``done`` 补到 ``total``）。

    ``worker``：见 :func:`_owned_clause`——陈旧 worker 的收尾不该把新一轮改成完成。
    """
    stamp = time.time() if now is None else now
    clause, params = _owned_clause(
        "status = ?, progress = 1.0,"
        " done = CASE WHEN total > 0 THEN total ELSE done END,"
        " note = CASE WHEN ? = '' THEN note ELSE ? END,"
        " error = NULL, lease_until = NULL, worker = NULL, finished_at = ?, updated_at = ?",
        (DONE, note, note, stamp, stamp),
        job_id,
        worker,
    )
    with transaction() as conn:
        conn.execute(f"UPDATE jobs SET {clause}", params)


def fail(
    job_id: int,
    *,
    error: str,
    retryable: bool,
    kind: str = "unknown",
    detail: str = "",
    worker: str | None = None,
    now: float | None = None,
) -> str:
    """记一次失败：还能重试就回 ``pending``（退避），否则 ``failed``。

    参数：
        error: 给人看的失败原因（界面直接显示）。
        retryable: 这次失败值不值得重试（由 :class:`app.core.jobs.model.JobError` 判定）。
        kind: **失败分类**（§10.1 的九类，或 interrupted/cancelled/unknown）——存进 `error_kind`，
            让"是哪一类问题"也能被统计与筛选（以前只存了一句人话，分类用完就丢了）。
        detail: 完整原因 / traceback（界面折叠显示）。
        worker: 认领者令牌；陈旧 worker 的失败写入会被忽略（那一轮已经被回收了）。
    返回：任务落到的新状态（``pending`` / ``failed`` / ``stale``）。
    """
    stamp = time.time() if now is None else now
    with transaction() as conn:
        row = conn.execute(
            "SELECT status, fails, max_attempts, worker FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return FAILED
        if worker is not None and row["worker"] not in (None, worker):
            # 这一轮已经被回收/重新认领：旧线程的失败不该污染新一轮
            _insert_event(
                conn, job_id, kind="stale",
                message="陈旧 worker 的失败写入已忽略", detail=error, now=stamp,
            )
            return "stale"
        fails = int(row["fails"]) + 1
        limit = max(1, int(row["max_attempts"] or 1))
        _insert_event(
            conn, job_id, kind=kind, message=error, detail=detail, attempt=fails, now=stamp
        )
        if retryable and fails < limit:
            delay = min(BACKOFF_BASE_S * (2 ** (fails - 1)), BACKOFF_MAX_S)
            conn.execute(
                "UPDATE jobs SET status = ?, fails = ?, error = ?, error_kind = ?, next_run_at = ?,"
                " lease_until = NULL, worker = NULL, updated_at = ? WHERE id = ?",
                (PENDING, fails, error, kind, stamp + delay, stamp, job_id),
            )
            return PENDING
        conn.execute(
            "UPDATE jobs SET status = ?, fails = ?, error = ?, error_kind = ?, lease_until = NULL,"
            " worker = NULL, finished_at = ?, updated_at = ? WHERE id = ?",
            (FAILED, fails, error, kind, stamp, stamp, job_id),
        )
        return FAILED


def retry_row(job_id: int, *, now: float | None = None) -> bool:
    """手动重试：把 ``failed`` / ``cancelled`` 的任务重新排队。

    ``fails`` 归零——手动重试的意思是"再给它全新的机会"，而不是"再试最后一次"。
    """
    stamp = time.time() if now is None else now
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status = ?, fails = 0, error = NULL, cancel_requested = 0,"
            " lease_until = NULL, worker = NULL, started_at = NULL, finished_at = NULL,"
            f" next_run_at = ?, updated_at = ? WHERE id = ? AND status IN ('{FAILED}', '{CANCELLED}')",
            (PENDING, stamp, stamp, job_id),
        )
        return bool(cursor.rowcount)


def request_cancel(job_id: int, *, now: float | None = None) -> bool:
    """请求取消。

    - ``pending``：**立刻**变 ``cancelled``（还没开始跑，没什么可收尾的）；
    - ``running``：只置 ``cancel_requested``，任务在**批次边界**响应（界面显示「正在取消…」）。

    **状态判断与写入必须在同一条 UPDATE 里**：先读 status 再按它分支，会出现
    "读到 pending、写的时候已经是 running"的 TOCTOU——那会把正在跑的任务直接改成
    cancelled，而 worker 还在跑（最后 finish 又覆盖回 done），取消语义被破坏。
    """
    stamp = time.time() if now is None else now
    with transaction() as conn:
        # ① 还在排队：直接取消（条件里带 status，避免覆盖刚被认领成 running 的那条）
        pending_done = conn.execute(
            "UPDATE jobs SET status = ?, cancel_requested = 1, note = ?, finished_at = ?,"
            " updated_at = ? WHERE id = ? AND status = ?",
            (CANCELLED, "已取消", stamp, stamp, job_id, PENDING),
        ).rowcount
        if pending_done:
            return True
        # ② 正在跑：只置标志，交给 handler 在批次边界收尾
        running_done = conn.execute(
            "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ? AND status = ?",
            (stamp, job_id, RUNNING),
        ).rowcount
        return bool(running_done)


def settle_cancelled(
    job_id: int, *, note: str = "已取消", worker: str | None = None, now: float | None = None
) -> None:
    """handler 响应完取消之后，把 ``running`` 收尾成 ``cancelled``（由 runner 调用）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        _insert_event(conn, job_id, kind="cancelled", message="用户取消", now=stamp)
        clause, params = _owned_clause(
            "status = ?, note = ?, lease_until = NULL, worker = NULL,"
            " finished_at = ?, updated_at = ?",
            (CANCELLED, note, stamp, stamp),
            job_id,
            worker,
        )
        conn.execute(f"UPDATE jobs SET {clause}", params)


def requeue_interrupted(
    job_id: int,
    *,
    note: str = "上次被关窗口中断",
    delay_s: float = 1.0,
    worker: str | None = None,
    now: float | None = None,
) -> None:
    """优雅退出：把在跑的任务放回 ``pending``（**不算失败**，``fails`` 不 +1）。

    ``runs`` **减一**：这次认领不算数。否则"崩溃上限"（`runs - fails >= CRASH_LIMIT`）会把
    被正常关窗口打断过几次的任务算成崩溃——用户关四次窗口 + 真崩一次，任务就被判死了。
    减一之后 `runs` 的含义是"**有效**认领次数"，崩溃上限也才对得上。

    ``delay_s`` 默认 1 秒：刚被中断的任务**不该立刻又被抓走**——否则"认领 → 中断 → 放回 →
    再认领"会变成一个疯狂打日志的死循环（这个坑真踩过：一分钟刷了 12 万行）。
    """
    stamp = time.time() if now is None else now
    with transaction() as conn:
        job = conn.execute("SELECT runs, fails, worker FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return
        if worker is not None and job["worker"] not in (None, worker):
            # 陈旧 worker：这一轮已经被回收，别把新一轮又放回队列
            return
        _insert_event(
            conn,
            job_id,
            kind="interrupted",
            message=note,
            detail="应用退出时任务还没收尾，已放回队列（不算失败）",
            attempt=int(job["fails"] or 0),
            now=stamp,
        )
        conn.execute(
            "UPDATE jobs SET status = ?, note = ?, lease_until = NULL, worker = NULL, started_at = NULL,"
            " runs = max(0, runs - 1), next_run_at = ?, updated_at = ? WHERE id = ?",
            (PENDING, note, stamp + max(0.0, delay_s), stamp, job_id),
        )


def is_cancelled(job_id: int) -> bool:
    """用户是否请求了取消（``running`` 时看标志位，已经取消的看状态）。"""
    row = get_connection().execute(
        "SELECT status, cancel_requested FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return True  # 行都没了（被清理）→ 当作"别再跑了"
    return bool(row["cancel_requested"]) or str(row["status"]) == CANCELLED


def purge_finished(*, now: float | None = None) -> int:
    """清理已完成的任务：留 7 天，且至少留最近 200 条。返回删除条数。"""
    stamp = time.time() if now is None else now
    statuses = ", ".join(f"'{status}'" for status in FINISHED_STATUSES)
    with transaction() as conn:
        cursor = conn.execute(
            f"DELETE FROM jobs WHERE status IN ({statuses}) AND updated_at < ?"
            f" AND id NOT IN (SELECT id FROM jobs WHERE status IN ({statuses})"
            " ORDER BY updated_at DESC LIMIT ?)",
            (stamp - KEEP_FINISHED_DAYS * 86400.0, KEEP_FINISHED_ROWS),
        )
        return int(cursor.rowcount or 0)


def delete_finished(*, now: float | None = None) -> int:
    """**立刻**删掉所有已完成/失败/取消的任务（界面上「清理已完成」用它）。

    与 :func:`purge_finished` 的区别：那个是"保留策略"（留 7 天且至少留 200 条，启动时顺手做），
    这个是"我现在就要清干净"。曾经把两者混用，结果按钮点了什么也没发生（保留策略对刚完成的
    任务不生效）——**界面上的按钮必须做它字面承诺的事**。
    """
    del now  # 立即清理不需要时间参数，保留形参只是为了与 purge_finished 一致
    statuses = ", ".join(f"'{status}'" for status in FINISHED_STATUSES)
    with transaction() as conn:
        cursor = conn.execute(f"DELETE FROM jobs WHERE status IN ({statuses})")
        return int(cursor.rowcount or 0)


def pending_of_unknown_kinds(known: tuple[str, ...], *, limit: int = 200) -> list[Job]:
    """待跑任务里**类型不在已知列表**的那些（注册表变过、老版本留下的僵尸任务）。

    为什么用 SQL 而不是"取 500 条 pending 再在 Python 里过滤"：队列可能积上万条，
    每 10 秒把整段排队区拉进内存是白费（而且那些任务本来就不该被认领）。
    """
    if known:
        placeholders = ",".join("?" for _ in known)
        clause = f"kind NOT IN ({placeholders})"
        params: tuple[Any, ...] = (*known, max(1, limit))
    else:  # 一个 kind 都没注册（启动早期）：全部待跑的都算"类型不存在"
        clause = "1 = 1"
        params = (max(1, limit),)
    rows = get_connection().execute(
        f"SELECT * FROM jobs WHERE status = ? AND {clause} LIMIT ?", (PENDING, *params)
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def oldest_pending_at() -> float | None:
    """最早入队且还在排队的任务的时间（判"队列是不是卡住了"）。"""
    row = get_connection().execute(
        f"SELECT min(created_at) AS oldest FROM jobs WHERE status = '{PENDING}'"
    ).fetchone()
    if row is None or row["oldest"] is None:
        return None
    return float(row["oldest"])


def delete_jobs(job_ids: list[int], *, only_finished: bool = True) -> int:
    """删掉指定任务（事件级联删）。返回删除条数。

    参数：
        only_finished: **默认只删已结束的**——排队/在跑的是用户还在等的事，误删等于把活弄丢了。
    """
    if not job_ids:
        return 0
    placeholders = ",".join("?" for _ in job_ids)
    clause = f"id IN ({placeholders})"
    if only_finished:
        statuses = ",".join(f"'{status}'" for status in FINISHED_STATUSES)
        clause = f"{clause} AND status IN ({statuses})"
    with transaction() as conn:
        cursor = conn.execute(f"DELETE FROM jobs WHERE {clause}", tuple(job_ids))
        return int(cursor.rowcount or 0)


def next_due_at() -> float | None:
    """最近一条"该跑时刻"（没有待跑任务就返回 None）。

    调度器据此**睡到点**而不是固定间隔空转；也被测试用来验证退避时间。
    """
    row = get_connection().execute(
        f"SELECT min(next_run_at) AS due FROM jobs WHERE status = '{PENDING}' AND cancel_requested = 0"
    ).fetchone()
    if row is None or row["due"] is None:
        return None
    return float(row["due"])


def count_jobs(
    *,
    statuses: tuple[str, ...] | None = None,
    kind: str | None = None,
    finished_since: float | None = None,
) -> int:
    """数任务（分页要用的总数；走索引，别用"查出来再 len()"）。

    参数：
        finished_since: 只要**在这个时刻之后结束**的任务（退出收尾行用它统计"本次运行完成了多少"）。
    """
    where: list[str] = []
    params: list[Any] = []
    if statuses:
        where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if finished_since is not None:
        where.append("finished_at IS NOT NULL AND finished_at >= ?")
        params.append(finished_since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    row = get_connection().execute(f"SELECT count(*) AS n FROM jobs {clause}", tuple(params)).fetchone()
    return int(row["n"]) if row else 0


def list_jobs(
    *,
    statuses: tuple[str, ...] | None = None,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
    newest_first: bool = False,
) -> list[Job]:
    """列任务（分页用：``limit`` + ``offset``）。

    参数：
        newest_first: 按**完成时间**倒序（"已结束"那类列表要它）；否则正在跑 → 排队 → 已结束，
            同组内按优先级与时间排（调度视角的顺序）。
    """
    where: list[str] = []
    params: list[Any] = []
    if statuses:
        where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = "updated_at DESC, id DESC" if newest_first else (
        f"{_STATUS_ORDER}, priority DESC, next_run_at, updated_at DESC"
    )
    rows = get_connection().execute(
        f"SELECT * FROM jobs {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, max(1, int(limit)), max(0, int(offset))),
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def stats() -> dict[str, int]:
    """各状态的计数（任务页徽标与概览用）。"""
    counts = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0, CANCELLED: 0}
    for row in get_connection().execute("SELECT status, count(*) AS n FROM jobs GROUP BY status"):
        counts[str(row["status"])] = int(row["n"])
    counts["active"] = counts[PENDING] + counts[RUNNING]
    return counts


def get_paused() -> bool:
    """队列是否处于暂停（**持久化**：用户暂停后重启仍是暂停）。"""
    row = get_connection().execute(
        "SELECT value_json FROM settings WHERE key = ?", (PAUSED_KEY,)
    ).fetchone()
    if row is None:
        return False
    try:
        return bool(json.loads(str(row["value_json"])))
    except ValueError:
        return False


def set_paused(value: bool, *, now: float | None = None) -> None:
    """写暂停状态。"""
    stamp = time.time() if now is None else now
    payload = json.dumps(bool(value))
    with transaction() as conn:
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
            " updated_at = excluded.updated_at",
            (PAUSED_KEY, payload, stamp),
        )
