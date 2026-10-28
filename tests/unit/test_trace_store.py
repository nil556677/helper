"""trace 存储与记录器：写读、fail-open、裁剪。"""

from __future__ import annotations

import time

import pytest

from app.core.db import schema_v1
from app.core.db.connection import connect
from app.core.trace import recorder
from app.core.trace import store as trace_store


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


def test_run_span_hit_event_lifecycle(fake_root):
    run_id = "run-1"
    trace_store.create_run(run_id, question="合同违约金怎么算", conversation_id="c1")
    trace_store.update_run(run_id, rewritten_query="合同 违约金 标准", model="test")
    span_id = trace_store.add_span(run_id, "route", "node", input={"k": 1})
    assert isinstance(span_id, int) and span_id > 0
    trace_store.finish_span(span_id, status="ok", output={"count": 5})
    trace_store.add_hits(
        run_id,
        [
            {
                "stage": "dense",
                "rank": 1,
                "chunk_key": "1:1:2",
                "score": 0.8,
                "score_kind": "cosine",
                "text": "x" * 5000,  # 超长要截断
            }
        ],
        span_id=span_id,
    )
    trace_store.add_event(run_id, level="warning", code="rewrite_timeout", message="超时")
    trace_store.finish_run(run_id, status="ok", prompt_tokens=10, completion_tokens=5)

    detail = trace_store.detail(run_id)
    assert detail is not None
    assert detail.run.status == "ok"
    assert detail.run.total_tokens == 15
    assert detail.run.duration_ms is not None
    assert len(detail.spans) == 1
    assert detail.spans[0].status == "ok"
    assert len(detail.hits) == 1
    assert len(detail.hits[0].text) <= 4000
    assert len(detail.events) == 1


def test_recorder_context_and_fail_open(fake_root):
    collected: list[str] = []

    class Marker:
        pass

    with recorder.trace_run(question="q", conversation_id="c2") as session:
        assert recorder.current_trace() is session
        with recorder.trace_span("retrieve_dense", "retrieval") as span:
            span.set_output({"count": 3})
        recorder.trace_hit(stage="dense", rank=1, chunk_key="k")
        recorder.trace_event(code="ok", message="fine")
        collected.append(session.run_id)

    assert len(collected) == 1
    run_id = collected[0]
    detail = trace_store.detail(run_id)
    assert detail is not None
    assert detail.run.status == "ok"
    assert len(detail.spans) == 1
    assert len(detail.hits) == 1
    assert len(detail.events) == 1

    # 没有活跃会话时打点是无害的
    recorder.trace_hit(stage="dense", rank=1)
    recorder.trace_event(code="x", message="y")


def test_trace_run_marks_error_on_exception(fake_root):
    run_id = "run-err"
    with pytest.raises(RuntimeError, match="boom"), recorder.trace_run(
        question="q", conversation_id="c3"
    ) as session:
        run_id = session.run_id
        raise RuntimeError("boom")
    detail = trace_store.detail(run_id)
    assert detail is not None
    assert detail.run.status == "error"
    assert "boom" in detail.run.error_detail


def test_prune_and_clear(fake_root):
    for i in range(3):
        trace_store.create_run(f"old-{i}", question=f"q{i}", started_at=time.time() - 4000 * 86400)
    trace_store.create_run("new", question="new", started_at=time.time())
    assert trace_store.prune(days=30) >= 3
    assert trace_store.detail("new") is not None
    assert trace_store.detail("old-0") is None

    trace_store.create_run("another", question="another", started_at=time.time())
    removed = trace_store.clear()
    assert removed >= 1
    assert trace_store.has_running() is False


def test_has_running_and_list_filters(fake_root):
    trace_store.create_run("run-a", question="天气", started_at=time.time())
    trace_store.create_run("run-b", question="订单", started_at=time.time())
    trace_store.finish_run("run-b", status="error", error_kind="timeout")

    assert trace_store.has_running() is True
    rows, total = trace_store.list_runs(days=30, status="all", query="", only_search=False, page=1, size=20)
    assert total == 2
    assert [r.id for r in rows] == ["run-b", "run-a"]

    rows, total = trace_store.list_runs(days=30, status="error", query="", only_search=False, page=1, size=20)
    assert total == 1 and rows[0].id == "run-b"

    rows, total = trace_store.list_runs(days=30, status="all", query="订单", only_search=False, page=1, size=20)
    assert total == 1 and rows[0].id == "run-b"

    stats = trace_store.stats(days=30)
    assert stats["total"] == 2
    assert stats["ok"] == 0        # run-a 未结束（running），run-b 是 error
    assert stats["failed"] == 1
    assert stats["p50_ms"] is not None
