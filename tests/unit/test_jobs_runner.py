"""任务执行器：泳道并发、结果落地、优雅退出、崩溃恢复、界面延迟让路。

用 **inline 模式**（`tick()` 在调用线程里同步跑）做确定性用例；只有"并发上限""优雅退出"
这些必须用真线程的才写成带时间余量的用例。

**起真线程的用例，收尾必须等线程结束**（`runner.stop(grace_s=…)` 给足时间）：
worker 线程取数据库连接时是**每次现算路径**的（正是这一点让用例之间能隔离），
所以一个还活着的 worker 会跟着下一个用例的 monkeypatch 跑进**下一个用例的库**，
把它的任务 #1 标成 done——踩过一次（表现为另一个用例莫名失败）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.jobs import api, registry, store
from app.core.jobs.model import (
    CANCELLED,
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    JobContext,
    JobError,
)
from app.core.jobs.runner import Runner


@pytest.fixture
def jobs_db(fake_root: Path) -> Iterator[None]:
    """建好 schema v2 的空库 + 干净注册表。"""
    from app.core import paths
    from app.core.db import schema
    from app.core.db.connection import close_thread_connection

    paths.ensure_dirs()
    schema.init_db()
    registry.clear()
    try:
        yield
    finally:
        registry.clear()
        close_thread_connection()


def _kind(key: str = "work", *, lane: str = "cpu", handler=None, **kwargs) -> registry.JobKind:
    spec = registry.JobKind(
        key=key,
        title=key,
        lane=lane,
        handler=handler or (lambda ctx: None),
        **kwargs,
    )
    registry.register(spec)
    return spec


# ---------------- 结果落地（inline，确定） ----------------


def test_inline_tick_runs_handler_and_marks_done(jobs_db: None) -> None:
    """跑通一条：handler 收到 payload、能报进度、返回值落进 note，状态变 done。"""
    seen: dict[str, object] = {}

    def handler(ctx: JobContext) -> str:
        seen["payload"] = ctx.payload
        ctx.report(3, 3, "3/3")
        return "切出 3 个片段"

    _kind(handler=handler)
    job_id = api.enqueue("work", {"doc": 7})

    runner = Runner(inline=True)
    claimed = runner.tick()

    assert [job.id for job in claimed] == [job_id]
    assert seen["payload"] == {"doc": 7}
    job = store.get_job(job_id)
    assert job.status == DONE
    assert job.note == "切出 3 个片段"
    assert job.progress == pytest.approx(1.0)


def test_inline_drain_runs_everything(jobs_db: None) -> None:
    """drain 把能跑的都跑完（一次 tick 至少推进一条）。"""
    calls: list[int] = []
    _kind(handler=lambda ctx: calls.append(ctx.job_id) or None)
    for _ in range(3):
        api.enqueue("work")

    assert Runner(inline=True).drain() == 3
    assert len(calls) == 3
    assert api.stats()[DONE] == 3


def test_handler_error_retries_then_fails(jobs_db: None) -> None:
    """业务失败：先退避重试，用尽 `max_attempts` 才判失败。"""

    def handler(ctx: JobContext) -> None:
        raise JobError("网络超时", kind="timeout")

    _kind(handler=handler, max_attempts=2)
    job_id = api.enqueue("work")

    runner = Runner(inline=True)
    runner.tick()
    assert store.get_job(job_id).status == PENDING  # 等退避

    runner.tick(now=time.time() + 10_000.0)
    job = store.get_job(job_id)
    assert job.status == FAILED
    assert job.error == "网络超时" and job.fails == 2


def test_auth_error_fails_immediately(jobs_db: None) -> None:
    """key 不对这类失败**一次就判失败**：重试只是让用户多等。"""

    def handler(ctx: JobContext) -> None:
        raise JobError("密钥不对", kind="auth")

    _kind(handler=handler, max_attempts=3)
    job_id = api.enqueue("work")
    Runner(inline=True).tick()

    job = store.get_job(job_id)
    assert job.status == FAILED and job.fails == 1


def test_non_idempotent_kind_never_retries(jobs_db: None) -> None:
    """不幂等的任务不给自动重试（重跑会造成重复副作用）。"""

    def handler(ctx: JobContext) -> None:
        raise RuntimeError("炸了")

    _kind(handler=handler, idempotent=False, max_attempts=5)
    job_id = api.enqueue("work")
    Runner(inline=True).tick()

    assert store.get_job(job_id).status == FAILED


def test_unexpected_exception_is_retryable(jobs_db: None) -> None:
    """没预料到的异常（比如代码 bug）先重试——多半是偶发。"""

    def handler(ctx: JobContext) -> None:
        raise RuntimeError("偶发")

    _kind(handler=handler, max_attempts=3)
    job_id = api.enqueue("work")
    Runner(inline=True).tick()

    job = store.get_job(job_id)
    assert job.status == PENDING and "偶发" in str(job.error)


def test_cancel_from_within_handler(jobs_db: None) -> None:
    """协作式取消：handler 在批次边界 report 时发现被取消 → 收尾成 cancelled，并跑清理钩子。"""
    cleaned: list[int] = []

    def handler(ctx: JobContext) -> None:
        api.cancel(ctx.job_id)  # 用户在界面上点了取消
        ctx.report(1, 10)  # 下一个批次边界响应

    _kind(handler=handler, on_cancel=lambda ctx: cleaned.append(ctx.job_id))
    job_id = api.enqueue("work")
    Runner(inline=True).tick()

    assert store.get_job(job_id).status == CANCELLED
    assert cleaned == [job_id]


def test_request_stop_requeues_instead_of_failing(jobs_db: None) -> None:
    """应用要退出：任务回队列，**不算失败**（`fails` 不涨）。"""

    def handler(ctx: JobContext) -> None:
        ctx.report(1, 10)

    _kind(handler=handler)
    job_id = api.enqueue("work")

    runner = Runner(inline=True)
    runner.request_stop()
    runner.tick()

    job = store.get_job(job_id)
    assert job.status == PENDING
    assert job.fails == 0
    assert job.note == "上次被关窗口中断"


def test_start_recovers_running_jobs(jobs_db: None) -> None:
    """启动先做崩溃恢复：上次留在 running 的任务被收回（不等租约）。"""
    _kind()
    job_id = api.enqueue("work")
    claimed = store.claim_next(("work",), lease_s=600.0, now=time.time() + 1.0)
    assert claimed is not None
    assert store.get_job(job_id).status == RUNNING

    Runner(inline=True).start()
    assert store.get_job(job_id).status == PENDING


def test_maintenance_fails_orphan_jobs(jobs_db: None) -> None:
    """类型已经没注册的待跑任务不该永远躺在"排队"里。"""
    job_id = store.enqueue("ghost", {"x": 1}, now=time.time())[0]
    _kind()  # 只注册 work

    outcome = Runner(inline=True).maintenance()
    assert outcome["orphans"] == 1
    job = store.get_job(job_id)
    assert job.status == FAILED and "已经不存在" in str(job.error)


# ---------------- 并发上限与让路（真线程） ----------------


def test_payload_version_mismatch_fails_clearly(jobs_db: None) -> None:
    """程序升级后 payload 结构变了：队列里旧格式的任务要**明确判失败**，不能拿错参数硬跑。"""
    _kind(payload_version=2)
    job_id, _created = store.enqueue("work", {"old": True}, payload_version=1)

    Runner(inline=True).tick()

    job = store.get_job(job_id)
    assert job.status == FAILED
    assert "旧版本" in str(job.error)
    assert [event.kind for event in store.events(job_id)] == ["unsupported"]


def test_unresponsive_job_is_left_to_next_startup(jobs_db: None) -> None:
    """宽限期内没收尾、**线程还活着**的任务：不在退出时改状态（改了会造成真双跑）。

    它留给"下次启动的无条件回收"，结果一样，但不会出现"状态是 pending、线程还在写数据"。
    """

    def stubborn(ctx: JobContext) -> None:
        time.sleep(3.0)  # 一整批，期间不 report

    _kind(handler=stubborn, lease_s=30.0)
    job_id = api.enqueue("work")

    runner = Runner(inline=False, lane_limits={"cpu": 1, "io": 2})
    runner.start()
    runner.wake()
    deadline = time.monotonic() + 3.0
    while store.get_job(job_id).status != RUNNING and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.get_job(job_id).status == RUNNING

    runner.stop(grace_s=0.3)  # 线程还在睡，宽限期不够

    assert store.get_job(job_id).status == RUNNING, "线程还活着时不该把状态改回 pending"
    store.recover_on_start()  # 下次启动接手
    assert store.get_job(job_id).status == PENDING

    runner.stop(grace_s=3.5)  # 等那个不合作的线程睡完（否则它会跑进下一个用例的库）


def test_maintenance_warns_when_queue_is_stuck(jobs_db: None) -> None:
    """队列卡住（有任务排了很久没轮到）要在日志里喊一声，而不是静默地没反应。"""
    _kind()
    store.enqueue("work", {}, now=1_700_000_000.0)
    runner = Runner(inline=True)

    fresh = runner.maintenance(now=1_700_000_010.0)
    assert fresh["stuck"] is False

    stuck = runner.maintenance(now=1_700_000_000.0 + 400.0)
    assert stuck["stuck"] is True

    # 恢复后要重新武装（否则第二次卡住就不喊了）
    assert runner.maintenance(now=1_700_000_000.0 + 401.0)["stuck"] is True
    store.finish(store.list_jobs()[0].id)
    assert runner.maintenance(now=1_700_000_500.0)["stuck"] is False


def test_wait_next_sleeps_to_the_next_due_time(jobs_db: None, monkeypatch) -> None:
    """兜底睡眠的算法：没活 → 睡满空闲间隔；已到期 → 按活跃间隔；未到期 → **精确睡到那一刻**。

    这块是"不是固定间隔空转"的核心（退避重试精确到点、投递毫秒级被叫醒），
    以前只靠间接用例覆盖，现在直接量它睡多久。
    """
    from app.core.jobs import runner as runner_module

    monkeypatch.setattr(runner_module, "IDLE_SLEEP_S", 0.25)
    monkeypatch.setattr(runner_module, "ACTIVE_POLL_S", 0.15)
    runner = Runner(inline=True)

    def elapsed_for(due: float | None) -> float:
        monkeypatch.setattr(store, "next_due_at", lambda: due)
        start = time.monotonic()
        runner._wait_next()
        return time.monotonic() - start

    assert 0.2 <= elapsed_for(None) < 0.6, "没活时该睡满空闲间隔"
    assert 0.1 <= elapsed_for(time.time() - 5) < 0.5, "已到期该按活跃间隔轮询"
    assert 0.2 <= elapsed_for(time.time() + 0.3) < 0.6, "未到期该精确睡到那一刻"


def test_loop_survives_a_failing_tick(jobs_db: None, monkeypatch) -> None:
    """调度线程**不许死**：`tick()` 抛异常也要记下来继续跑（否则队列从此不动，界面还显示"运行中"）。

    用直接调 `_loop()`（而不是 `start()`）来保证用例确定：`_wait_next` 换成空操作，
    `tick` 前两次抛异常、第三次请求停止——三次都跑到就说明异常没把循环带走。
    """
    runner = Runner(inline=True)
    calls = {"n": 0}

    def flaky_tick() -> list:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("模拟一次偶发错误")
        runner.request_stop()
        return []

    monkeypatch.setattr(runner, "tick", flaky_tick)
    monkeypatch.setattr(runner, "_wait_next", lambda: None)

    runner._loop()  # 直接跑循环（正常情况它跑在自己的线程里）

    assert calls["n"] == 3, "异常不该让调度循环退出"


def test_stop_logs_and_leaves_nothing_running(jobs_db: None) -> None:
    """优雅退出之后：没有在跑的任务、库里也没有僵尸 `running`。"""
    _kind(handler=lambda ctx: time.sleep(1.5), lease_s=30.0)
    job_id = api.enqueue("work")

    runner = Runner(inline=False, lane_limits={"cpu": 1, "io": 2})
    runner.start()
    runner.wake()
    deadline = time.monotonic() + 3.0
    while store.get_job(job_id).status != RUNNING and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.get_job(job_id).status == RUNNING

    runner.stop(grace_s=0.4)
    assert api.stats()["running"] <= 1  # 线程还活着的会留给下次启动回收
    store.recover_on_start()
    assert api.stats()["running"] == 0, "启动恢复之后不该再有 running"

    # **收尾要等线程真的结束**：worker 里的库路径是每次调用现算的（正是这一点让用例之间能隔离），
    # 所以一个还活着的 worker 会跟着下一个用例的 monkeypatch 跑进**下一个用例的库**，
    # 把它的任务 #1 标成 done——踩过这个坑（表现为"暂停冻结"用例莫名失败）。
    runner.stop(grace_s=2.0)


def test_cpu_lane_runs_one_at_a_time(jobs_db: None) -> None:
    """cpu 泳道并发 1：第二条必须在第一条跑完前不被认领（实测依据：并发 4 会让界面 p95 到 113ms）。"""
    active = 0
    peak = 0
    lock = threading.Lock()

    def handler(ctx: JobContext) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with lock:
            active -= 1

    _kind(handler=handler)
    for _ in range(2):
        api.enqueue("work")

    runner = Runner(inline=False, lane_limits={"cpu": 1, "io": 2})
    runner.start()
    runner.wake()
    deadline = time.monotonic() + 5.0
    while api.stats()[DONE] < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    runner.stop(grace_s=1.0)

    assert peak == 1, f"cpu 泳道同时跑了 {peak} 条"
    assert api.stats()[DONE] == 2


def test_handler_error_keeps_classification_and_traceback(jobs_db: None) -> None:
    """执行器要把**失败分类**记下来；没预料到的异常还要留下 traceback。"""

    def explained(ctx: JobContext) -> None:
        raise JobError("密钥不对", kind="auth")

    _kind(handler=explained, max_attempts=3)
    job_id = api.enqueue("work")
    Runner(inline=True).tick()

    job = store.get_job(job_id)
    assert job.status == FAILED and job.error_kind == "auth"
    assert [event.kind for event in store.events(job_id)] == ["auth"]

    def crashed(ctx: JobContext) -> None:
        raise RuntimeError("下标越界")

    registry.clear()
    _kind(key="boom", handler=crashed, max_attempts=1)
    boom = api.enqueue("boom")
    Runner(inline=True).tick()

    events = store.events(boom)
    assert events and events[0].kind == "unknown"
    assert "Traceback" in events[0].detail and "下标越界" in events[0].detail


def test_pause_freezes_running_job(jobs_db: None) -> None:
    """暂停：正在跑的任务在批次边界**挂起**（进度不再前进），恢复后继续。

    这是方案 §13.5 承诺的行为——第一次做出来时**没实现**（探针实测发现暂停期间任务照跑）。
    """

    def handler(ctx: JobContext) -> None:
        for step in range(1, 500):
            ctx.report(step, 500, f"{step}/500")
            time.sleep(0.05)

    _kind(handler=handler, lease_s=10.0)
    job_id = api.enqueue("work")

    runner = Runner(inline=False, lane_limits={"cpu": 1, "io": 2})
    runner.start()
    runner.wake()
    deadline = time.monotonic() + 3.0
    while store.get_job(job_id).status != RUNNING and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.get_job(job_id).status == RUNNING

    api.pause()
    time.sleep(0.5)  # 让它走到批次边界并挂起
    frozen = store.get_job(job_id).done
    time.sleep(0.8)
    assert store.get_job(job_id).done == frozen, "暂停期间进度不该继续前进"

    api.resume()
    time.sleep(0.7)
    assert store.get_job(job_id).done > frozen, "恢复后应该继续跑"

    runner.stop(grace_s=2.0)


def test_lag_provider_pauses_cpu_lane(jobs_db: None) -> None:
    """界面延迟高时 cpu 泳道自动让路，恢复正常后自己回来。

    去抖：**连续两次**偏高才降级（一次渲染尖峰不该让任务停）；但只要一次正常就**立刻**恢复
    （恢复要快——实测启动那次是"暂停后要等 10 秒才回来"，现在检查独立成 1 秒一次）。
    """
    lag = {"ms": 500.0}
    calls: list[int] = []
    _kind(handler=lambda ctx: calls.append(ctx.job_id) or None)
    api.enqueue("work")

    runner = Runner(inline=True, lag_provider=lambda: lag["ms"])
    assert runner.check_lag() is False, "第一次偏高还不够（去抖）"
    assert runner.check_lag() is True, "连续两次偏高 → 让路"
    assert runner.tick() == []  # 让路期间不认领
    assert calls == []

    lag["ms"] = 10.0
    assert runner.check_lag() is False, "一次正常立刻恢复"
    assert len(runner.tick()) == 1


def test_io_lane_runs_two_at_a_time(jobs_db: None) -> None:
    """io 泳道并发 2（等网络的任务可以并行，不吃 CPU）。"""
    active = 0
    peak = 0
    lock = threading.Lock()

    def handler(ctx: JobContext) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.2)
        with lock:
            active -= 1

    _kind(key="fetch", lane="io", handler=handler)
    for _ in range(2):
        api.enqueue("fetch")

    runner = Runner(inline=False, lane_limits={"cpu": 1, "io": 2})
    runner.start()
    runner.wake()
    deadline = time.monotonic() + 5.0
    while api.stats()[DONE] < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    runner.stop(grace_s=1.0)

    assert peak == 2, f"io 泳道只跑了 {peak} 条"


def test_graceful_stop_requeues_running(jobs_db: None) -> None:
    """优雅退出：正在跑的任务收到停止信号后回队列（`fails` 不涨），下次启动接着做。"""

    def handler(ctx: JobContext) -> None:
        for step in range(1, 200):
            ctx.report(step, 200, f"{step}/200")
            time.sleep(0.01)

    _kind(handler=handler, lease_s=5.0)
    job_id = api.enqueue("work")

    runner = Runner(inline=False, lane_limits={"cpu": 1, "io": 2})
    runner.start()
    runner.wake()
    deadline = time.monotonic() + 3.0
    while store.get_job(job_id).status != RUNNING and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.get_job(job_id).status == RUNNING

    runner.stop(grace_s=2.0)

    job = store.get_job(job_id)
    assert job.status == PENDING
    assert job.fails == 0
