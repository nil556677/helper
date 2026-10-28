"""检索追踪（净室 v2）：模型 / 存储 / 记录器。

- `app/core/trace/model.py`：dataclass 形状（`TraceRun` / `TraceSpan` / `TraceHit` / `TraceEvent`）；
- `app/core/trace/store.py`：SQLite 读写（唯一入口，纯 DB）；
- `app/core/trace/recorder.py`：contextvars 驱动的会话记录器（节点/LLM/检索打点入口）。

分层：`core` 只碰 SQLite，不碰 LangChain/LangGraph/UI。
"""

from __future__ import annotations

from app.core.trace.model import (
    TraceEvent,
    TraceHit,
    TraceRun,
    TraceSpan,
)
from app.core.trace.recorder import (
    TraceSession,
    current_trace,
    trace_event,
    trace_hit,
    trace_run,
    trace_span,
)
from app.core.trace.store import (
    add_event,
    add_hits,
    add_span,
    clear,
    create_run,
    detail,
    finish_run,
    finish_span,
    has_running,
    list_runs,
    prune,
    stats,
    update_run,
)

__all__ = [
    "TraceEvent",
    "TraceHit",
    "TraceRun",
    "TraceSession",
    "TraceSpan",
    "add_event",
    "add_hits",
    "add_span",
    "clear",
    "create_run",
    "current_trace",
    "detail",
    "finish_run",
    "finish_span",
    "has_running",
    "list_runs",
    "prune",
    "stats",
    "trace_event",
    "trace_hit",
    "trace_run",
    "trace_span",
    "update_run",
]
