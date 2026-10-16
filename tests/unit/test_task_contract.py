"""任务契约：统一结果格式、统一参数、异步执行、全局并发上限、统一记录。

**为什么单独一个文件**：这些是"任务写法的硬约束"（§8.2），跟"发现与注册"（test_task_discovery）
是两件事。这里每条都对应一个明确的承诺：

1. **结果必须统一**：``run`` 返回 :class:`TaskResult`，否则明确判失败（不重试——那是代码 bug）；
2. **参数统一**：类里声明 ``Params``，入队那一刻校验；多余字段被丢掉；执行时按同一形状解析；
3. **异步支持**：``async def run`` 与普通 ``def run`` 都能跑；
4. **并发有上限**：全局同时执行数不超过上限（泳道之上再加一道总闸）；
5. **统一记录**：结果摘要、结果详情、执行耗时都留得下来，界面拿得到。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel

from app.core import paths
from app.core.jobs import api, registry
from app.core.jobs.model import DONE, FAILED, Job
from app.core.jobs.registry import JobKind
from app.core.jobs.runner import Runner
from app.core.jobs.task import Task, TaskResult, clear_kind_cache
from app.services import tasks as tasks_service

# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


@pytest.fixture
def jobs_db(fake_root: Path) -> Iterator[None]:
    """空库 + 干净注册表（每个用例自己注册需要的任务类）。"""
    from app.core.db import schema
    from app.core.db.connection import close_thread_connection

    paths.ensure_dirs()
    schema.init_db()
    registry.clear()
    clear_kind_cache()
    try:
        yield
    finally:
        registry.clear()
        clear_kind_cache()
        close_thread_connection()


def _run_inline(job_id: int) -> Job:
    """把队列跑干，返回该任务的最新状态。"""
    Runner(inline=True).drain()
    job = api.get(job_id)
    assert job is not None
    return job


# ---------------------------------------------------------------------------
# 结果统一格式
# ---------------------------------------------------------------------------


def test_result_note_is_recorded(jobs_db: None) -> None:
    """``TaskResult.note`` 落成任务的结果（界面直接显示那一句）。"""

    class NoteTask(Task):
        title = "有结果的任务"

        def run(self, ctx: object, params: object) -> TaskResult:
            return TaskResult(note="干完了")

    registry.register(NoteTask.to_kind())
    job = _run_inline(api.enqueue("note", {}))

    assert job.status == DONE
    assert job.note == "干完了"


def test_result_detail_goes_to_event_timeline(jobs_db: None) -> None:
    """``TaskResult.detail`` 写进事件时间线（界面"详情"里能看到），而不是被丢掉。"""

    class DetailTask(Task):
        title = "有详情的任务"

        def run(self, ctx: object, params: object) -> TaskResult:
            return TaskResult(note="完成", detail="清理了 3 个目录：a、b、c")

    registry.register(DetailTask.to_kind())
    job_id = api.enqueue("detail", {})
    _run_inline(job_id)

    events = api.events(job_id)
    result_events = [event for event in events if event.kind == "result"]
    assert result_events, [(e.kind, e.message) for e in events]
    assert "清理了 3 个目录" in result_events[0].detail
    # 界面上这类事件要有人话标签
    assert tasks_service.KIND_LABELS.get("result") == "执行结果"


def test_wrong_return_type_fails_fast_and_never_retries(jobs_db: None) -> None:
    """返回的不是 TaskResult → 明确判失败，且**不重试**（返回格式错是代码 bug）。"""

    class BadTask(Task):
        title = "返回格式不对的任务"

        def run(self, ctx: object, params: object) -> TaskResult:
            return "我只是个字符串"  # type: ignore[return-value]

    registry.register(BadTask.to_kind())
    job = _run_inline(api.enqueue("bad", {}))

    assert job.status == FAILED
    assert job.fails == 1, "格式错误不该耗掉重试机会"
    assert "TaskResult" in str(job.error), job.error


# ---------------------------------------------------------------------------
# 参数统一
# ---------------------------------------------------------------------------


class PayloadedTask(Task):
    """一个带参数形状的任务（下面几个用例共用）。"""

    title = "带参数的任务"

    class Params(BaseModel):
        name: str
        count: int = 1

    seen: ClassVar[list[object]] = []

    def run(self, ctx: object, params: PayloadedTask.Params) -> TaskResult:
        PayloadedTask.seen.append(params)
        return TaskResult(note=f"{params.name} × {params.count}")


def test_params_are_validated_at_enqueue(jobs_db: None) -> None:
    """参数不合法 → **入队那一刻**就被拒（不是排半天队再失败）。"""
    registry.register(PayloadedTask.to_kind())

    with pytest.raises(ValueError, match="name"):
        api.enqueue("payloaded", {"count": 2})          # 缺必填
    with pytest.raises(ValueError, match="count"):
        api.enqueue("payloaded", {"name": "x", "count": "不是数字"})


def test_unknown_params_are_dropped(jobs_db: None) -> None:
    """多余字段被丢掉：**参数不需要那么多**，库里只留任务真正声明过的那几个。"""
    registry.register(PayloadedTask.to_kind())
    job_id = api.enqueue("payloaded", {"name": "甲", "junk": 123, "another": "x"})

    job = api.get(job_id)
    assert job is not None
    assert set(job.payload) == {"name", "count"}, job.payload
    assert job.payload["name"] == "甲"
    assert job.payload["count"] == 1, "没给的字段用声明里的默认值"


def test_params_are_passed_to_run_as_model(jobs_db: None) -> None:
    """执行时拿到的是**声明好的模型对象**（有类型、有默认值），不是裸 dict。"""
    PayloadedTask.seen.clear()
    registry.register(PayloadedTask.to_kind())
    job = _run_inline(api.enqueue("payloaded", {"name": "乙", "count": 3}))

    assert job.status == DONE
    assert job.note == "乙 × 3"
    assert len(PayloadedTask.seen) == 1
    passed = PayloadedTask.seen[0]
    assert isinstance(passed, PayloadedTask.Params)
    assert (passed.name, passed.count) == ("乙", 3)


def test_old_format_payload_fails_with_clear_reason(jobs_db: None) -> None:
    """库里躺着老格式的排队任务（绕过 api 直接写库）→ 明确判失败并说清原因。"""
    from app.core.jobs import store

    registry.register(PayloadedTask.to_kind())
    job_id, _ = store.enqueue("payloaded", {"legacy_field": 1}, max_attempts=3)
    job = _run_inline(job_id)

    assert job.status == FAILED
    assert "参数不合法" in str(job.error)
    assert job.fails == 1, "参数格式问题不该重试"


# ---------------------------------------------------------------------------
# 异步执行
# ---------------------------------------------------------------------------


def test_async_task_runs(jobs_db: None) -> None:
    """``async def run`` 也支持（在工作线程里用独立事件循环跑完）。"""

    class AsyncTask(Task):
        title = "异步任务"

        async def run(self, ctx: object, params: object) -> TaskResult:  # type: ignore[override]
            import asyncio

            await asyncio.sleep(0)
            return TaskResult(note="异步完成")

    registry.register(AsyncTask.to_kind())
    job = _run_inline(api.enqueue("async", {}))

    assert job.status == DONE
    assert job.note == "异步完成"


def test_async_task_error_is_recorded(jobs_db: None) -> None:
    """异步任务里抛的错同样被记录（不会静默消失）。"""

    class AsyncBoom(Task):
        title = "异步失败"
        idempotent = False

        async def run(self, ctx: object, params: object) -> TaskResult:  # type: ignore[override]
            raise RuntimeError("异步里炸了")

    registry.register(AsyncBoom.to_kind())
    job = _run_inline(api.enqueue("async_boom", {}))

    assert job.status == FAILED
    assert "异步里炸了" in str(job.error)


# ---------------------------------------------------------------------------
# 全局并发上限
# ---------------------------------------------------------------------------


def test_global_concurrency_cap(jobs_db: None) -> None:
    """全局同时执行数不超过上限（泳道上限放到 5，也不许超过总闸 2）。"""
    gate = threading.Event()
    started: list[int] = []

    def handler(ctx: object) -> str:
        started.append(ctx.job_id)  # type: ignore[attr-defined]
        gate.wait(timeout=5)
        return "ok"

    registry.register(JobKind(key="blocker", title="占位任务", lane="cpu", handler=handler))
    for index in range(5):
        api.enqueue("blocker", {"i": index}, dedupe_key=f"block:{index}")

    runner = Runner(lane_limits={"cpu": 5}, max_concurrent=2)
    try:
        runner.tick()
        deadline = time.time() + 3
        while len(started) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert len(started) == 2, f"总闸是 2，却同时跑了 {len(started)} 个"
        assert runner.running_count == 2
        assert runner.max_concurrent == 2
    finally:
        gate.set()
        runner.stop(grace_s=2.0)


def test_concurrency_is_visible_to_the_service(jobs_db: None) -> None:
    """界面能拿到"并发 x/y"（没有执行器时也要给个安全答案）。"""
    info = tasks_service.concurrency()
    assert isinstance(info, tasks_service.Concurrency)
    assert info.limit >= 1
    assert info.running >= 0


# ---------------------------------------------------------------------------
# 统一记录：耗时 / 参数 / 结果都能从服务层拿到
# ---------------------------------------------------------------------------


def test_service_exposes_duration_params_and_result(jobs_db: None) -> None:
    """服务层把"执行时间、参数、结果"都给出来（界面据此展示全）。"""
    job = Job(
        id=7,
        kind="import",
        payload={"name": "合同.pdf", "path": "data/inbox/x"},
        payload_version=1,
        status=DONE,
        priority=0,
        progress=1.0,
        done=1,
        total=1,
        note="切出 12 个片段",
        runs=1,
        fails=0,
        max_attempts=3,
        cursor=None,
        resource=None,
        cancel_requested=False,
        next_run_at=0.0,
        lease_until=None,
        worker=None,
        error=None,
        error_kind=None,
        created_at=100.0,
        started_at=110.0,
        finished_at=115.5,
        updated_at=116.0,
    )

    view = tasks_service.TaskView.from_job(job)

    assert view.duration_s == pytest.approx(5.5)
    assert view.result == "切出 12 个片段"
    keys = {item.key for item in view.params}
    assert keys == {"name", "path"}, view.params
    assert view.started_at == 110.0 and view.finished_at == 115.5
