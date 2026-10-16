"""任务的落库操作：**所有 SQL 都在这里**（引擎其它模块不许直接写 jobs 表）。

三条来自实测与评审的设计事实（技术方案 §13.4、§13.5）：

- **认领索引必须带 ``priority DESC``**：否则 SQLite 会为整个 pending 队列建临时 B 树
  （实测队列 10000 条时认领从 1.02ms 变成 2.14ms）；
- **``runs`` 与 ``fails`` 分开**：崩溃（网络抖动、被强杀）不该耗掉业务重试机会，
  否则"一次抖动 + 两次崩溃"会把一条本来能成功的任务冤枉判死；
- **一次只认领一条**（``LIMIT 1``）：崩溃时最多丢一条的租约，恢复代价最小。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from app.core.db.connection import get_connection, transaction
from app.core.jobs.model import (
    ACTIVE_STATUSES,
    CANCELLED,
    DONE,
    FAILED,
    FINISHED_STATUSES,
    PENDING,
    RUNNING,
    Job,
    JobEvent,
)

#: 事件表里单条文本的长度上限（traceback 可能几万字符，事件表不该被一条记录撑爆）
MESSAGE_MAX_CHARS = 500
DETAIL_MAX_CHARS = 8000

#: 连续崩溃（认领了却没好好结束）超过它 → 判"反复崩溃"，不再重跑
CRASH_LIMIT = 5

#: 退避：``base × 2^(fails-1)``，上限 5 分钟
BACKOFF_BASE_S = 5.0
BACKOFF_MAX_S = 300.0

#: 保留策略：完成的任务留 7 天，且至少留最近 200 条
KEEP_FINISHED_ROWS = 200
KEEP_FINISHED_DAYS = 7.0

#: 队列暂停状态存的键（settings 表；与 app/services/preferences 同一张表、同样 JSON 编码）
PAUSED_KEY = "jobs.paused"

#: 状态排序：任务页按"正在跑 → 排队 → 已结束"排
_STATUS_ORDER = f"CASE status WHEN '{RUNNING}' THEN 0 WHEN '{PENDING}' THEN 1 ELSE 2 END"


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
        stamp,
        stamp,
    )
    try:
        with transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(kind, payload, payload_version, dedupe_key, status, priority,"
                " next_run_at, max_attempts, resource, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            return int(cursor.lastrowid or 0), True
    except sqlite3.IntegrityError:
        # 部分唯一索引挡住了"活跃期重复排队"——这是**预期行为**，返回已有那条
        existing = active_id_for(dedupe_key, kind) if dedupe_key else None
        if existing is None:
            raise
        return existing, False


def active_id_for(dedupe_key: str, kind: str) -> int | None:
    """找一条活跃（排队或在跑）的同 dedupe 任务。"""
    row = get_connection().execute(
        f"SELECT id FROM jobs WHERE kind = ? AND dedupe_key = ? AND status IN"
        f" ('{PENDING}', '{RUNNING}') ORDER BY id LIMIT 1",
        (kind, dedupe_key),
    ).fetchone()
    return int(row["id"]) if row else None


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


def write_progress(
    job_id: int, done: int, total: int, note: str, *, lease_s: float, now: float | None = None
) -> None:
    """写进度并**续租**（handler 调用 ``report()`` 时走这里）。

    进度按批写、不逐条写：10 万次写库是不可接受的（``JobContext`` 里有 0.2 秒节流）。
    """
    stamp = time.time() if now is None else now
    progress = 0.0 if not total else min(1.0, max(0.0, done / total))
    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET done = ?, total = ?, progress = ?, note = ?, lease_until = ?, updated_at = ?"
            f" WHERE id = ? AND status = '{RUNNING}'",
            (int(done), int(total), progress, note, stamp + float(lease_s), stamp, job_id),
        )


def set_cursor(job_id: int, value: str, *, now: float | None = None) -> None:
    """记下断点续跑位置（立刻落库：它就是为"崩了以后接着跑"准备的）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        conn.execute("UPDATE jobs SET cursor = ?, updated_at = ? WHERE id = ?", (value, stamp, job_id))


def finish(job_id: int, *, note: str = "", now: float | None = None) -> None:
    """标记成功（``progress`` 直接满格，``done`` 补到 ``total``）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, progress = 1.0,"
            " done = CASE WHEN total > 0 THEN total ELSE done END,"
            " note = CASE WHEN ? = '' THEN note ELSE ? END,"
            " error = NULL, lease_until = NULL, worker = NULL, finished_at = ?, updated_at = ?"
            " WHERE id = ?",
            (DONE, note, note, stamp, stamp, job_id),
        )


def _insert_event(
    conn: Any,
    job_id: int,
    *,
    kind: str,
    message: str,
    detail: str = "",
    attempt: int = 0,
    now: float | None = None,
) -> None:
    """往事件表写一笔（**必须在调用方的事务里**：状态与事件要么一起成、要么一起不成）。"""
    conn.execute(
        "INSERT INTO job_events(job_id, at, attempt, kind, message, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (
            job_id,
            time.time() if now is None else now,
            int(attempt),
            kind,
            _clip(message),
            _clip(detail, limit=DETAIL_MAX_CHARS),
        ),
    )


def _clip(text: str, *, limit: int = MESSAGE_MAX_CHARS) -> str:
    """截断过长的文本（traceback 可能几万字符；事件表不该被一条记录撑爆）。"""
    text = str(text or "")
    return text if len(text) <= limit else f"{text[:limit]}…（已截断）"


def events(job_id: int, *, limit: int = 50) -> list[JobEvent]:
    """某个任务的事件时间线（新的在后；界面按顺序读）。"""
    rows = get_connection().execute(
        "SELECT * FROM job_events WHERE job_id = ? ORDER BY id LIMIT ?", (job_id, max(1, int(limit)))
    ).fetchall()
    return [JobEvent.from_row(row) for row in rows]


def fail(
    job_id: int,
    *,
    error: str,
    retryable: bool,
    kind: str = "unknown",
    detail: str = "",
    now: float | None = None,
) -> str:
    """记一次失败：还能重试就回 ``pending``（退避），否则 ``failed``。

    参数：
        error: 给人看的失败原因（界面直接显示）。
        retryable: 这次失败值不值得重试（由 :class:`app.core.jobs.model.JobError` 判定）。
        kind: **失败分类**（§10.1 的九类，或 interrupted/cancelled/unknown）——存进 `error_kind`，
            让"是哪一类问题"也能被统计与筛选（以前只存了一句人话，分类用完就丢了）。
        detail: 完整原因 / traceback（界面折叠显示）。
    返回：任务落到的新状态（``pending`` 或 ``failed``）。
    """
    stamp = time.time() if now is None else now
    with transaction() as conn:
        row = conn.execute("SELECT fails, max_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return FAILED
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
    """
    stamp = time.time() if now is None else now
    job = get_job(job_id)
    if job is None or job.status not in ACTIVE_STATUSES:
        return False
    with transaction() as conn:
        if job.status == PENDING:
            conn.execute(
                "UPDATE jobs SET status = ?, cancel_requested = 1, note = ?, finished_at = ?, updated_at = ?"
                " WHERE id = ?",
                (CANCELLED, "已取消", stamp, stamp, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?", (stamp, job_id)
            )
    return True


def settle_cancelled(job_id: int, *, note: str = "已取消", now: float | None = None) -> None:
    """handler 响应完取消之后，把 ``running`` 收尾成 ``cancelled``（由 runner 调用）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        _insert_event(conn, job_id, kind="cancelled", message="用户取消", now=stamp)
        conn.execute(
            "UPDATE jobs SET status = ?, note = ?, lease_until = NULL, worker = NULL,"
            " finished_at = ?, updated_at = ? WHERE id = ?",
            (CANCELLED, note, stamp, stamp, job_id),
        )


def requeue_interrupted(
    job_id: int, *, note: str = "上次被关窗口中断", delay_s: float = 1.0, now: float | None = None
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
        job = conn.execute("SELECT runs, fails FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is not None:
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


def _recover(kinds_condition: str, params: tuple[Any, ...], *, now: float | None) -> dict[str, int]:
    """崩溃恢复的公共实现：先判"反复崩溃"，再把剩下的放回 pending（**每一条都记一笔事件**）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        affected = conn.execute(
            f"SELECT id, runs, fails FROM jobs WHERE {kinds_condition}", params
        ).fetchall()
        failed = conn.execute(
            "UPDATE jobs SET status = ?, error = '反复崩溃（已认领 ' || runs || ' 次），不再重跑',"
            " error_kind = 'unknown', lease_until = NULL, worker = NULL, finished_at = ?, updated_at = ?"
            f" WHERE {kinds_condition} AND runs - fails >= ?",
            (FAILED, stamp, stamp, *params, CRASH_LIMIT),
        ).rowcount
        recovered = conn.execute(
            "UPDATE jobs SET status = ?, note = '上次被中断，正在重跑', lease_until = NULL,"
            " worker = NULL, started_at = NULL, next_run_at = ?, updated_at = ?"
            f" WHERE {kinds_condition}",
            (PENDING, stamp, stamp, *params),
        ).rowcount
        for row in affected:
            crashed_hard = int(row["runs"] or 0) - int(row["fails"] or 0) >= CRASH_LIMIT
            _insert_event(
                conn,
                int(row["id"]),
                kind="crash",
                message="反复崩溃，不再重跑" if crashed_hard else "上次进程中断，已放回队列",
                detail="进程被强杀或断电时任务还在跑（租约到期/启动恢复时被回收）",
                attempt=int(row["fails"] or 0),
                now=stamp,
            )
    return {"recovered": int(recovered or 0), "failed": int(failed or 0)}


def recover_on_start(*, now: float | None = None) -> dict[str, int]:
    """启动时恢复：**无条件**把所有 ``running`` 收回 ``pending``。

    为什么不等租约过期：单进程应用，上次进程已死是**确定的事实**——等 60 秒只会让用户
    对着不动的任务页发呆。租约留给运行期的"线程卡死"。
    """
    return _recover(f"status = '{RUNNING}'", (), now=now)


def reclaim_expired(*, now: float | None = None) -> dict[str, int]:
    """运行期回收：租约过期的 ``running``（线程卡死/被杀）放回 ``pending``。"""
    stamp = time.time() if now is None else now
    return _recover(f"status = '{RUNNING}' AND lease_until IS NOT NULL AND lease_until < ?", (stamp,), now=now)


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


def get_job(job_id: int) -> Job | None:
    """按 id 取任务。"""
    row = get_connection().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


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
