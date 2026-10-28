"""设置 · 检索追踪：trace 的界面侧服务（UI-free 视图模型）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.trace import store as trace_store
from app.core.trace.model import TraceEvent, TraceHit, TraceRun, TraceSpan


@dataclass(frozen=True)
class TraceRow:
    id: str
    at: float
    question: str
    route_label: str
    mode: str
    hit_count: int
    duration_ms: int | None
    ttft_ms: int | None
    tokens: int
    status: str
    error_kind: str


@dataclass(frozen=True)
class TraceStatsView:
    total: int
    ok: int
    failed: int
    waiting: int
    search: int
    p50_ms: int | None
    p95_ms: int | None
    cache_rate: float
    tokens: int


@dataclass(frozen=True)
class TraceDetailView:
    run: TraceRun
    spans: tuple[TraceSpan, ...]
    hits: tuple[TraceHit, ...]
    events: tuple[TraceEvent, ...]


def _route_label(route_json: str) -> str:
    import json

    try:
        data = json.loads(route_json or "{}")
    except (ValueError, TypeError):
        return "—"
    intent = data.get("intent", "")
    tool = data.get("tool", "")
    if tool and tool != "search_kb":
        return f"{tool}"
    return intent or "—"


def _row(run: TraceRun, hit_count: int) -> TraceRow:
    return TraceRow(
        id=run.id,
        at=run.started_at,
        question=run.question[:120],
        route_label=_route_label(run.route_json),
        mode="hybrid",
        hit_count=hit_count,
        duration_ms=run.duration_ms,
        ttft_ms=run.ttft_ms,
        tokens=run.total_tokens,
        status=run.status,
        error_kind=run.error_kind,
    )


def page(*, days: int = 30, status: str = "all", query: str = "", only_search: bool = False,
         page_no: int = 1, size: int = 20) -> tuple[list[TraceRow], int]:
    runs, total = trace_store.list_runs(
        days=days, status=status, query=query, only_search=only_search,
        page=page_no, size=size,
    )
    counts = trace_store.hit_counts([run.id for run in runs])
    return [_row(run, counts.get(run.id, 0)) for run in runs], total


def stats(days: int = 1) -> TraceStatsView:
    data = trace_store.stats(days)
    return TraceStatsView(**data)


def error_top(days: int = 7, limit: int = 5) -> list[tuple[str, int]]:
    """失败原因 Top N（总览用）：`[(error_kind, 次数)]`。"""
    return trace_store.error_kinds(days=days, limit=limit)


def detail(run_id: str) -> TraceDetailView | None:
    detail_obj = trace_store.detail(run_id)
    if detail_obj is None:
        return None
    return TraceDetailView(
        run=detail_obj.run, spans=detail_obj.spans, hits=detail_obj.hits,
        events=detail_obj.events,
    )


def has_running() -> bool:
    return trace_store.has_running()


def clear() -> int:
    return trace_store.clear()


def prune(days: int = 30) -> int:
    return trace_store.prune(days)


def export_json(run_id: str) -> dict[str, Any] | None:
    view = detail(run_id)
    if view is None:
        return None
    from dataclasses import asdict

    return {
        "run": asdict(view.run),
        "spans": [asdict(span) for span in view.spans],
        "hits": [asdict(hit) for hit in view.hits],
        "events": [asdict(event) for event in view.events],
    }
