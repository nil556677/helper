"""任务引擎：排队、认领、进度、取消、重试、崩溃恢复、保留清理。

钉住的是**评审时挑出来的那几个真问题**（技术方案 §13）：

1. **崩溃与业务失败分开**（`runs` / `fails`）——网络抖动 + 崩溃不该把任务冤枉判死；
2. **不值得重试的失败立刻失败**——key 错不该白等 7 秒 ×3；
3. **启动无条件回收 `running`**——单进程应用，上次进程已死是确定事实，不该等 60 秒租约；
4. **dedupe 只约束活跃期**——不重复排队，跑完之后还能再排；
5. **认领是原子的**——两个线程抢同一条只成功一次；
6. **认领索引必须带 `priority DESC`**——用查询计划断言（否则队列一长就退化成临时 B 树）。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.db.connection import get_connection
from app.core.jobs import api, registry, store
from app.core.jobs.model import (
    CANCELLED,
    DONE,
    FAILED,
    PENDING,
    PRIORITY_MAINTENANCE,
    PRIORITY_WAITING,
    RUNNING,
    JobCancelled,
    JobContext,
    JobError,
)

SAMPLE_KIND = registry.JobKind(
    key="import", title="导入资料", lane="cpu", handler=lambda ctx: None, max_attempts=3, lease_s=60.0
)


@pytest.fixture
def jobs_db(fake_root: Path) -> Iterator[None]:
    """一个建好表（schema v2）、目录隔离在 ``.tmp/`` 下的空库 + 干净的注册表。"""
    from app.core import paths
    from app.core.db import schema
    from app.core.db.connection import close_thread_connection

    paths.ensure_dirs()
    schema.init_db()
    registry.clear()
    registry.register(SAMPLE_KIND)
    try:
        yield
    finally:
        registry.clear()
        close_thread_connection()


def _enqueue(**kwargs) -> int:
    return api.enqueue("import", {"doc": 7}, **kwargs)


def _claim(lease_s: float = 60.0, now: float | None = None):
    return store.claim_next(("import",), lease_s=lease_s, worker="test", now=now)


#: 固定时钟：要精确算租约/退避的用例用它（`api.enqueue` 走真实时钟，那是给功能用的路径）
FIXED = 1_700_000_000.0


def _enqueue_at(now: float = FIXED, **kwargs: object) -> int:
    """用固定时钟入队（其余用例仍走 api.enqueue，保证对外的路径也被测到）。"""
    job_id, _created = store.enqueue("import", {"doc": 7}, max_attempts=3, now=now, **kwargs)
    return job_id


def _now() -> float:
    """真实时钟 + 1 秒：给走 `api.enqueue`（真实时钟）的用例认领用。"""
    import time

    return time.time() + 1.0


def _context(job, *, stop=lambda: False, throttle_s: float = 0.0, lease_s: float = 60.0) -> JobContext:
    """按引擎的接法给 handler 造一个上下文（与 runner 里一致）。"""
    return JobContext(
        job_id=job.id,
        kind=job.kind,
        payload=job.payload,
        cursor=job.cursor,
        write_progress=lambda done, total, note: store.write_progress(
            job.id, done, total, note, lease_s=lease_s
        ),
        write_cursor=lambda value: store.set_cursor(job.id, value),
        is_cancelled=lambda: store.is_cancelled(job.id),
        should_stop=stop,
        throttle_s=throttle_s,
    )


# ---------------- schema 与索引 ----------------


def test_schema_has_jobs_table(jobs_db: None) -> None:
    """迁移把 jobs 表建起来了（老库升级与新库直建都要有）。

    **别写死版本号**：迁移一升版就会撞（v2 → v3 时就是这么撞了一次）。
    """
    from app.core.db.schema import SCHEMA_VERSION, init_schema, table_names

    assert SCHEMA_VERSION >= 2
    assert "jobs" in table_names(get_connection())
    assert init_schema(get_connection()) == SCHEMA_VERSION


def test_claim_index_covers_priority_order(jobs_db: None) -> None:
    """认领查询**不能**出现临时 B 树——这是实测抓到的性能坑（队列一长就线性变慢）。"""
    plan = get_connection().execute(
        "EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE status = 'pending' AND next_run_at <= 0"
        " ORDER BY priority DESC, next_run_at LIMIT 1"
    ).fetchall()
    text = " ".join(str(row[-1]) for row in plan)
    assert "TEMP B-TREE" not in text.upper(), text
    assert "idx_jobs_claim" in text, text


def test_dedupe_index_is_partial(jobs_db: None) -> None:
    """dedupe 是**部分**唯一索引：只约束活跃状态（跑完还能再排）。"""
    row = get_connection().execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_jobs_dedupe'"
    ).fetchone()
    assert row is not None
    assert "UNIQUE" in row["sql"].upper()
    assert "WHERE" in row["sql"].upper()


# ---------------- 排队与认领 ----------------


def test_enqueue_then_claim(jobs_db: None) -> None:
    """排队 → 认领：状态、认领次数与租约都要对。"""
    job_id = _enqueue_at()
    job = store.get_job(job_id)
    assert job is not None and job.status == PENDING

    claimed = _claim(now=FIXED)
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.status == RUNNING
    assert claimed.runs == 1
    assert claimed.lease_until == pytest.approx(FIXED + 60.0)
    assert claimed.worker == "test"
    assert claimed.payload == {"doc": 7}
    assert _claim(now=FIXED + 1) is None  # 已被认领，没人可认了


def test_enqueue_without_registered_kind_fails_loudly(jobs_db: None) -> None:
    """没注册过的 kind 不许入队（消息是人话，不是 KeyError 的裸文本）。"""
    with pytest.raises(KeyError) as caught:
        api.enqueue("nope")
    assert "没有注册的任务类型" in str(caught.value)


def test_claim_prefers_higher_priority(jobs_db: None) -> None:
    """优先级高的先跑（用户在等的活不能排在维护任务后面）。"""
    low = _enqueue_at(priority=PRIORITY_MAINTENANCE)
    high = _enqueue_at(priority=PRIORITY_WAITING)

    assert _claim(now=FIXED).id == high
    assert _claim(now=FIXED).id == low


def test_claim_respects_next_run_at(jobs_db: None) -> None:
    """还没到点的任务不许被认领（退避重试就是这么排的）。"""
    delayed = _enqueue_at(delay_s=60.0)
    assert _claim(now=FIXED) is None
    assert _claim(now=FIXED + 61.0).id == delayed


def test_claim_is_atomic_under_threads(jobs_db: None) -> None:
    """两个线程抢同一条任务：**只有一个**能拿到（认领靠一条 UPDATE，天然互斥）。"""
    job_id = _enqueue_at()
    winners: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(4)

    def worker() -> None:
        start.wait()
        claimed = _claim(now=FIXED)
        if claimed is not None:
            with lock:
                winners.append(claimed.id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert winners == [job_id]


def test_resource_exclusion(jobs_db: None) -> None:
    """独占资源（如 ONNX）：同名资源同时只允许一个任务在跑。"""
    spec = registry.JobKind(
        key="embed", title="向量化", lane="cpu", handler=lambda ctx: None, resource="onnx"
    )
    registry.register(spec)
    first = api.enqueue("embed", {"doc": 1})
    second = api.enqueue("embed", {"doc": 2})
    assert store.get_job(first).resource == "onnx", "kind 声明的独占资源要写进任务行"

    claimed_first = store.claim_next(("embed",), lease_s=60.0, now=_now())
    assert claimed_first is not None and claimed_first.id == first
    # 第一条占着 onnx：第二条（同资源）不能被认领
    assert store.claim_next(("embed",), lease_s=60.0, now=_now()) is None
    store.finish(claimed_first.id)
    assert store.claim_next(("embed",), lease_s=60.0, now=_now()).id == second


def test_dedupe_blocks_duplicates_until_done(jobs_db: None) -> None:
    """活跃期不重复排队；跑完之后可以再排（历史留得住）。"""
    first = _enqueue(dedupe_key="embed:doc:7")
    again = _enqueue(dedupe_key="embed:doc:7")
    assert again == first
    assert len(api.list_jobs()) == 1

    store.finish(first)
    third = _enqueue(dedupe_key="embed:doc:7")
    assert third != first
    assert len(api.list_jobs()) == 2  # 旧的完成记录还在


# ---------------- 进度、断点与取消 ----------------


def test_context_reports_progress_and_cursor(jobs_db: None) -> None:
    """`report()` 写进度并续租；`set_cursor()` 立刻落库（断点续跑靠它）。"""
    job_id = _enqueue_at()
    job = _claim(now=FIXED, lease_s=30.0)
    ctx = _context(job, lease_s=30.0)

    ctx.report(48, 120, "48/120 片段")
    ctx.set_cursor("page:48")

    fresh = store.get_job(job_id)
    assert (fresh.done, fresh.total) == (48, 120)
    assert fresh.note == "48/120 片段"
    assert fresh.progress == pytest.approx(0.4)
    assert fresh.cursor == "page:48"
    # 续租写成"真实现在 + 30 秒"（认领时用的是注入的 now=1000，所以这里要看它被推到了真实时间）
    assert fresh.lease_until is not None and fresh.lease_until > 1_000_000_000


def test_report_throttles_writes(jobs_db: None) -> None:
    """节流：短时间内多次 report 只写一次库（10 万次写库是不可接受的）。"""
    _enqueue()
    job = _claim()
    ctx = _context(job, throttle_s=60.0)
    for done in range(1, 100):
        ctx.report(done, 100, f"{done}/100")
    assert store.get_job(job.id).done == 1  # 只写了第一次

    ctx.flush()  # 收尾前一定要写下去
    assert store.get_job(job.id).done == 99


def test_cancel_pending_is_immediate(jobs_db: None) -> None:
    """排队中的任务取消是**立刻**的。"""
    job_id = _enqueue()
    assert api.cancel(job_id) is True
    job = store.get_job(job_id)
    assert job.status == CANCELLED and job.finished_at is not None
    assert _claim() is None


def test_cancel_running_is_cooperative(jobs_db: None) -> None:
    """在跑的任务只能**协作式**取消：请求置位 → 下一个 report() 抛 JobCancelled。"""
    job_id = _enqueue()
    job = _claim()
    assert api.cancel(job_id) is True
    assert store.get_job(job_id).status == RUNNING  # 还没停：等批次边界
    assert store.get_job(job_id).cancel_requested is True

    ctx = _context(job)
    with pytest.raises(JobCancelled):
        ctx.report(1, 10)
    store.settle_cancelled(job_id)
    assert store.get_job(job_id).status == CANCELLED


def test_interrupted_is_not_a_failure(jobs_db: None) -> None:
    """应用退出（JobInterrupted）不算失败：任务回 pending，`fails` 不涨。"""
    from app.core.jobs.model import JobInterrupted

    _enqueue()
    job = _claim()
    ctx = _context(job, stop=lambda: True)
    with pytest.raises(JobInterrupted):
        ctx.report(1, 10)

    store.requeue_interrupted(job.id)
    fresh = store.get_job(job.id)
    assert fresh.status == PENDING
    assert fresh.fails == 0
    assert fresh.note == "上次被关窗口中断"


# ---------------- 失败、重试与崩溃恢复 ----------------


def test_fail_retries_with_backoff_then_gives_up(jobs_db: None) -> None:
    """业务失败：退避重试，用尽 `max_attempts` 才判失败。"""
    job_id = _enqueue()
    _claim()

    assert store.fail(job_id, error="网络超时", retryable=True, now=1000.0) == PENDING
    first = store.get_job(job_id)
    assert first.fails == 1 and first.next_run_at == pytest.approx(1005.0)  # base 5 秒

    _claim(now=1005.0)
    assert store.fail(job_id, error="网络超时", retryable=True, now=1005.0) == PENDING
    second = store.get_job(job_id)
    assert second.fails == 2 and second.next_run_at == pytest.approx(1015.0)  # 10 秒

    _claim(now=1015.0)
    assert store.fail(job_id, error="网络超时", retryable=True, now=1015.0) == FAILED
    done = store.get_job(job_id)
    assert done.status == FAILED and done.error == "网络超时" and done.finished_at is not None


def test_auth_error_does_not_retry(jobs_db: None) -> None:
    """key 不对、地址不对这类失败**立刻失败**——重试只是让用户多等 7 秒。"""
    for kind in ("auth", "not_found", "bad_request", "config", "unsupported"):
        assert JobError("x", kind=kind).retryable is False
    assert JobError("x", kind="timeout").retryable is True
    assert JobError("x", kind="unknown", retryable=True).retryable is True  # 可以显式覆盖

    job_id = _enqueue()
    _claim()
    assert store.fail(job_id, error="密钥不对", retryable=False, now=1000.0) == FAILED


def test_crashes_do_not_consume_retries(jobs_db: None) -> None:
    """崩溃（认领了没结束）只涨 `runs`，不涨 `fails` —— 重试机会留给真正的失败。"""
    job_id = _enqueue()
    _claim()
    store.recover_on_start(now=1000.0)  # 模拟"进程被杀后重启"

    job = store.get_job(job_id)
    assert job.status == PENDING
    assert (job.runs, job.fails) == (1, 0)
    assert job.max_attempts == 3  # 三次重试机会一点没动


def test_recover_on_start_does_not_wait_for_lease(jobs_db: None) -> None:
    """启动恢复是**无条件**的：租约还没过期也要收回来（单进程应用，上次进程肯定没了）。"""
    import time

    _enqueue()
    claimed = _claim(now=time.time() + 1.0)  # 租约在"未来"（远未过期）
    assert claimed is not None and claimed.lease_until is not None
    assert claimed.lease_until > time.time()

    outcome = store.recover_on_start(now=time.time() + 2.0)
    assert outcome["recovered"] == 1
    assert store.get_job(claimed.id).status == PENDING


def test_repeated_crashes_give_up(jobs_db: None) -> None:
    """反复崩溃（`runs - fails` 超过上限）→ 判失败，不再无限重跑。"""
    job_id = _enqueue()
    for _ in range(store.CRASH_LIMIT + 1):
        _claim()
        store.recover_on_start()

    job = store.get_job(job_id)
    assert job.status == FAILED
    assert "反复崩溃" in str(job.error)


def test_reclaim_expired_only_touches_stale_leases(jobs_db: None) -> None:
    """运行期回收只看租约：没过期的不许动（它可能正在正常跑）。"""
    _enqueue_at()
    claimed = _claim(lease_s=30.0, now=FIXED)
    assert claimed is not None

    assert store.reclaim_expired(now=FIXED + 10.0)["recovered"] == 0  # 租约还有效
    assert store.get_job(claimed.id).status == RUNNING
    assert store.reclaim_expired(now=FIXED + 40.0)["recovered"] == 1  # 过期了
    assert store.get_job(claimed.id).status == PENDING


def test_manual_retry_resets_fails(jobs_db: None) -> None:
    """手动重试 = 再给一次全新的机会（`fails` 归零）。"""
    job_id = _enqueue()
    _claim()
    store.fail(job_id, error="网络超时", retryable=False)
    assert store.get_job(job_id).status == FAILED

    assert api.retry(job_id) is True
    fresh = store.get_job(job_id)
    assert fresh.status == PENDING and fresh.fails == 0 and fresh.error is None
    assert api.retry(job_id) is False  # 排队中/在跑的不给重试


# ---------------- 列表、统计、暂停、清理 ----------------


def test_list_jobs_orders_running_then_pending(jobs_db: None) -> None:
    """任务页的顺序：正在跑 → 排队 → 已结束。"""
    finished = _enqueue()
    store.finish(finished)
    running = _enqueue()
    _claim()
    pending = _enqueue()

    order = [job.id for job in api.list_jobs()]
    assert order == [running, pending, finished]


def test_stats_counts(jobs_db: None) -> None:
    """概览与徽标用的计数。"""
    _enqueue()
    done = _enqueue()
    store.finish(done)
    _enqueue()
    claimed = _claim()
    assert claimed is not None  # 认领走的是优先级与入队顺序，不假设是哪一条

    data = api.stats()
    assert data[RUNNING] == 1
    assert data[PENDING] == 1
    assert data[DONE] == 1
    assert data["active"] == 2


def test_pause_is_persisted(jobs_db: None) -> None:
    """暂停是持久状态（用户暂停后重启仍是暂停，不会被自动恢复惹恼）。"""
    assert api.is_paused() is False
    api.pause()
    assert api.is_paused() is True
    api.resume()
    assert api.is_paused() is False


def test_schema_v3_records_error_kind_and_events(jobs_db: None) -> None:
    """v3 给任务加了两样东西：失败**分类**（`error_kind`）与**事件表**（每一次失败都留得住）。

    为什么需要：`jobs.error` 只留得住"最后一次失败"，而可重试的任务会失败好几次——
    排查要看的是时间线。
    """
    from app.core.db.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 3
    columns = {row["name"] for row in get_connection().execute("PRAGMA table_info(jobs)")}
    assert "error_kind" in columns


def test_fail_keeps_kind_and_full_timeline(jobs_db: None) -> None:
    """失败要连**分类**与**完整原因**一起记下来，而且要经得起重试（历史不被覆盖）。"""
    job_id = _enqueue()
    _claim()
    store.fail(job_id, error="密钥不对", retryable=False, kind="auth", detail="Traceback: 认证明细…")

    job = store.get_job(job_id)
    assert job.error_kind == "auth"
    first = store.events(job_id)
    assert [event.kind for event in first] == ["auth"]
    assert "认证明细" in first[0].detail

    # 重试一次再失败：**两次都在**（这就是"完整记录"的意义）
    assert api.retry(job_id) is True
    _claim()
    store.fail(job_id, error="超时了", retryable=False, kind="timeout")

    kinds = [event.kind for event in store.events(job_id)]
    assert kinds == ["auth", "timeout"], kinds
    assert store.get_job(job_id).error_kind == "timeout"  # 分类跟着最后一次


def test_interrupted_and_cancelled_write_events(jobs_db: None) -> None:
    """关窗口、用户取消也各记一笔（它们不是失败，但排查时要知道发生过）。"""
    interrupted = _enqueue()
    _claim()
    store.requeue_interrupted(interrupted)
    assert [event.kind for event in store.events(interrupted)] == ["interrupted"]

    cancelled = _enqueue()
    _claim()
    store.settle_cancelled(cancelled)
    assert [event.kind for event in store.events(cancelled)] == ["cancelled"]


def test_crash_recovery_writes_events(jobs_db: None) -> None:
    """崩溃恢复（回收 running）也要留痕，否则时间线上会莫名少一段。"""
    job_id = _enqueue()
    _claim()
    store.recover_on_start()

    events = store.events(job_id)
    assert [event.kind for event in events] == ["crash"]


def test_deleting_a_job_cascades_its_events(jobs_db: None) -> None:
    """删任务要连带删掉它的事件（否则事件表会越积越多，还留着已删任务的痕迹）。"""
    job_id = _enqueue()
    _claim()
    store.fail(job_id, error="炸了", retryable=False, kind="unknown")
    assert store.events(job_id)

    assert store.delete_finished() == 1
    assert get_connection().execute("SELECT count(*) AS n FROM job_events").fetchone()["n"] == 0


def test_delete_finished_clears_immediately(jobs_db: None) -> None:
    """「清理已完成」必须**立刻**清干净（保留策略对刚完成的任务不生效——踩过这个坑）。"""
    finished = _enqueue()
    store.finish(finished)
    running = _enqueue()
    _claim()
    pending = _enqueue()

    assert store.delete_finished() == 1
    left = {job.id for job in api.list_jobs()}
    assert left == {running, pending}, left
    assert api.stats()[DONE] == 0


def test_graceful_requeue_does_not_count_as_crash(jobs_db: None) -> None:
    """被正常关窗口打断**不该**算进"反复崩溃"。

    以前 `runs` 只加不减：用户关四次窗口 + 真崩一次 → `runs - fails = 5` → 任务被判死。
    现在优雅退出会把这次认领**退回去**（`runs` 减一），崩溃上限才只数真崩溃。
    """
    job_id = _enqueue()
    for _ in range(4):  # 关四次窗口，每次都"认领 → 被中断 → 放回"
        _claim()
        store.requeue_interrupted(job_id, delay_s=0.0)  # 用例里不等那 1 秒
        assert store.get_job(job_id).runs == 0, "优雅退出不该留下认领计数"

    _claim()  # 第五次真的崩了（进程被强杀）
    store.recover_on_start()

    job = store.get_job(job_id)
    assert job.status == PENDING, f"关窗口不该把任务判死：{job.error}"
    assert job.runs == 1


def test_pending_of_unknown_kinds_only_returns_ghosts(jobs_db: None) -> None:
    """孤儿查询要只挑"类型没注册"的那些（别把整段排队区拉进内存再过滤）。"""
    known = _enqueue()
    ghost, _created = store.enqueue("ghost", {}, now=1_700_000_000.0)

    found = store.pending_of_unknown_kinds(("import",))
    assert [job.id for job in found] == [ghost]

    store.finish(known)
    assert store.pending_of_unknown_kinds(("import",)) == [store.get_job(ghost)]
    assert store.pending_of_unknown_kinds(()) != [], "一个 kind 都没注册时，全部待跑都算孤儿"


def test_oldest_pending_at_tracks_the_queue_head(jobs_db: None) -> None:
    """"队列卡住"的判据：最早排队那条的入队时间。"""
    assert store.oldest_pending_at() is None
    first = _enqueue_at(now=1_700_000_000.0)
    _enqueue_at(now=1_700_000_500.0)
    assert store.oldest_pending_at() == 1_700_000_000.0

    store.finish(first)
    assert store.oldest_pending_at() == 1_700_000_500.0


def test_delete_jobs_refuses_active_ones(jobs_db: None) -> None:
    """批量删任务：**只删已结束的**——排队/在跑的是用户还在等的事，误删等于把活弄丢了。"""
    pending = _enqueue()
    done = _enqueue()
    _claim()
    store.finish(done)

    assert store.delete_jobs([pending, done]) == 1
    assert store.get_job(pending) is not None, "排队中的不该被删掉"
    assert store.get_job(done) is None
    assert store.delete_jobs([]) == 0


def test_purge_finished_keeps_recent(jobs_db: None) -> None:
    """保留策略：太老的清掉，但至少留最近 200 条。"""
    old = _enqueue()
    store.finish(old, now=1000.0)

    fresh_ids = [_enqueue() for _ in range(3)]
    for job_id in fresh_ids:
        store.finish(job_id)

    removed = store.purge_finished(now=1000.0 + store.KEEP_FINISHED_DAYS * 86400.0 + 1)
    assert removed == 0  # 只有 4 条，没到"至少留 200 条"之外
    assert len(api.list_jobs()) == 4


def test_count_jobs_can_filter_by_finish_time(jobs_db: None) -> None:
    """按结束时间过滤：退出收尾行靠它统计「本次运行完成了多少」。"""
    before = 1_700_000_000.0
    first = _enqueue_at(before)
    job = _claim(now=before)
    assert job is not None
    store.finish(first, note="跑完了", now=before + 10)

    second = _enqueue_at(before + 100)
    claimed = _claim(now=before + 100)
    assert claimed is not None
    store.fail(second, error="坏了", retryable=False, now=before + 110)

    # 全部（不限时间）
    assert store.count_jobs(statuses=("done",), finished_since=0) >= 1
    # 只看"运行开始之后"结束的：第一个在这次运行之前，第二个在这次之内
    assert store.count_jobs(statuses=("done",), finished_since=before + 50) == 0
    assert store.count_jobs(statuses=("failed",), finished_since=before + 50) == 1
    # 没有结束时间的任务不该被算进来（finished_at IS NULL）
    assert store.count_jobs(statuses=("pending",), finished_since=0) == 0
