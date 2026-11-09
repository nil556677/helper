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


def compaction_errors(conversation_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """该会话的历史记忆压缩失败（持久化于 trace_events，重启可查）。"""
    from app.core.db.connection import get_connection  # DB

    if not conversation_id:  # 无会话
        return []  # 空
    try:  # 坏库/旧表兜底
        rows = get_connection().execute(  # 关联 run 过滤会话
            "SELECT e.at, e.detail_json FROM trace_events e"
            " JOIN trace_runs r ON r.id = e.run_id"
            " WHERE r.conversation_id = ? AND e.code = 'compaction_failed'"
            " ORDER BY e.at DESC LIMIT ?",
            (conversation_id, int(limit)),
        ).fetchall()
    except Exception:  # 无表/坏库
        return []  # 空
    import json  # 解析 detail

    result: list[dict[str, Any]] = []  # 结果
    for item in rows:  # 逐条
        detail = {}  # 明细
        try:  # 解析
            payload = json.loads(item["detail_json"] or "{}")  # JSON
            detail = payload if isinstance(payload, dict) else {}  # dict
        except Exception:  # 坏 JSON
            detail = {}  # 空
        result.append(  # 追加
            {"conversation_id": conversation_id, "at": float(item["at"] or 0), "error": str(detail.get("error") or "")}
        )  # 结束
    return result  # 返回


def context_usage_stats(days: int = 7) -> dict[str, int]:
    """近 days 天累积的上下文剪枝/预算丢/缓存前缀 token（读 messages.turn_json.context_usage）。"""
    from app.core.db.connection import get_connection  # DB

    import json  # 解析 turn_json
    import time  # 时间窗

    since = time.time() - int(days) * 86400  # 起始时间
    total = {"pruned": 0, "dropped": 0, "cache_prefix_tokens": 0}  # 累积
    try:  # 坏库/旧表兜底
        rows = get_connection().execute(  # 近 days 天的 assistant 消息
            "SELECT turn_json FROM messages WHERE role = 'assistant' AND created_at >= ?",
            (since,),
        ).fetchall()
    except Exception:  # 无表
        return total  # 空
    for item in rows:  # 逐条
        try:  # 坏 JSON 跳过
            payload = json.loads(item["turn_json"] or "{}")  # turn_json
            usage = payload.get("context_usage") or {}  # 上下文占用
        except Exception:  # 坏 JSON
            continue  # 跳过
        if not isinstance(usage, dict):  # 非 dict
            continue  # 跳过
        for key in ("pruned", "dropped"):  # 剪枝/预算丢（dict 或 int）
            value = usage.get(key)  # 值
            if isinstance(value, dict):  # 按层 dict
                total[key] += int(sum(int(v) for v in value.values()))  # 求和
            elif value:  # int
                total[key] += int(value)  # 累加
        total["cache_prefix_tokens"] += int(usage.get("cache_prefix_tokens") or 0)  # 缓存前缀
    return total  # 返回
