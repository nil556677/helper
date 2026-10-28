"""trace 的视图形状（dataclass，与 UI 解耦）。

这里只放"界面/服务层能直接读"的值对象；SQL 全在 :mod:`app.core.trace.store`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TraceRun:
    """一轮提问/检索的摘要（列表页一行）。"""

    id: str
    parent_run_id: str = ""
    conversation_id: str = ""
    kind: str = "chat"
    status: str = "running"
    question: str = ""
    normalized_query: str = ""
    rewritten_query: str = ""
    route_json: str = "{}"
    answer: str = ""
    model: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    duration_ms: int | None = None
    ttft_ms: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit: bool = False
    error_kind: str = ""
    error_detail: str = ""
    metadata_json: str = "{}"

    @classmethod
    def from_row(cls, row: Any) -> TraceRun:
        return cls(
            id=str(row["id"]),
            parent_run_id=str(row["parent_run_id"] or ""),
            conversation_id=str(row["conversation_id"] or ""),
            kind=str(row["kind"] or "chat"),
            status=str(row["status"] or "running"),
            question=str(row["question"] or ""),
            normalized_query=str(row["normalized_query"] or ""),
            rewritten_query=str(row["rewritten_query"] or ""),
            route_json=str(row["route_json"] or "{}"),
            answer=str(row["answer"] or ""),
            model=str(row["model"] or ""),
            started_at=float(row["started_at"] or 0.0),
            finished_at=row["finished_at"],
            duration_ms=row["duration_ms"],
            ttft_ms=row["ttft_ms"],
            prompt_tokens=int(row["prompt_tokens"] or 0),
            completion_tokens=int(row["completion_tokens"] or 0),
            total_tokens=int(row["total_tokens"] or 0),
            cache_hit=bool(row["cache_hit"]),
            error_kind=str(row["error_kind"] or ""),
            error_detail=str(row["error_detail"] or ""),
            metadata_json=str(row["metadata_json"] or "{}"),
        )


@dataclass(frozen=True)
class TraceSpan:
    """时间线里的一条（一个节点/一次 LLM 调用/一次检索）。"""

    id: int
    run_id: str
    parent_span_id: int | None = None
    name: str = ""
    kind: str = "node"
    status: str = "running"
    started_at: float = 0.0
    duration_ms: int | None = None
    input_json: str = "{}"
    output_json: str = "{}"
    error_json: str = ""

    @classmethod
    def from_row(cls, row: Any) -> TraceSpan:
        return cls(
            id=int(row["id"]),
            run_id=str(row["run_id"]),
            parent_span_id=row["parent_span_id"],
            name=str(row["name"] or ""),
            kind=str(row["kind"] or "node"),
            status=str(row["status"] or "running"),
            started_at=float(row["started_at"] or 0.0),
            duration_ms=row["duration_ms"],
            input_json=str(row["input_json"] or "{}"),
            output_json=str(row["output_json"] or "{}"),
            error_json=str(row["error_json"] or ""),
        )


@dataclass(frozen=True)
class TraceHit:
    """一次召回里的一条命中（后台命中表的一行）。"""

    id: int
    run_id: str
    span_id: int | None = None
    stage: str = ""
    rank: int = 0
    chunk_id: int | None = None
    chunk_key: str = ""
    doc_id: int | None = None
    source_name: str = ""
    heading: str = ""
    loc_json: str = "{}"
    score: float | None = None
    score_kind: str = ""
    selected: bool = False
    text: str = ""
    snippet: str = ""

    @classmethod
    def from_row(cls, row: Any) -> TraceHit:
        return cls(
            id=int(row["id"]),
            run_id=str(row["run_id"]),
            span_id=row["span_id"],
            stage=str(row["stage"] or ""),
            rank=int(row["rank"] or 0),
            chunk_id=row["chunk_id"],
            chunk_key=str(row["chunk_key"] or ""),
            doc_id=row["doc_id"],
            source_name=str(row["source_name"] or ""),
            heading=str(row["heading"] or ""),
            loc_json=str(row["loc_json"] or "{}"),
            score=row["score"],
            score_kind=str(row["score_kind"] or ""),
            selected=bool(row["selected"]),
            text=str(row["text"] or ""),
            snippet=str(row["snippet"] or ""),
        )


@dataclass(frozen=True)
class TraceEvent:
    """结构化事件（超时/降级/缓存/工具停用/WARNING+ 日志）。"""

    id: int
    run_id: str = ""
    at: float = 0.0
    level: str = "info"
    code: str = ""
    message: str = ""
    detail_json: str = "{}"

    @classmethod
    def from_row(cls, row: Any) -> TraceEvent:
        return cls(
            id=int(row["id"]),
            run_id=str(row["run_id"] or ""),
            at=float(row["at"] or 0.0),
            level=str(row["level"] or "info"),
            code=str(row["code"] or ""),
            message=str(row["message"] or ""),
            detail_json=str(row["detail_json"] or "{}"),
        )


@dataclass(frozen=True)
class TraceDetail:
    """一次运行的完整视图（详情页）。"""

    run: TraceRun
    spans: tuple[TraceSpan, ...] = ()
    hits: tuple[TraceHit, ...] = ()
    events: tuple[TraceEvent, ...] = ()
