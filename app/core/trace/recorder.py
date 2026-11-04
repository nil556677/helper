"""trace 记录器：contextvars 驱动的会话与打点。

用法（净室 v2）：

```python
with trace_run(question="合同违约金怎么算", conversation_id="conv-1") as run:
    with trace_span("retrieve_dense", "retrieval", input={"k": 40}) as span:
        docs = ...
        span.set_output({"count": len(docs)})
    trace_hit("dense", rank=1, chunk_key="1:1:2", score=0.8, score_kind="cosine")
    trace_event(code="rewrite_timeout", message="重写超时", level="warning")
    # 图停在 interrupt 时：
    run.set_status("waiting")
```

**fail-open**：任何 store 写失败都只记日志，绝不向调用方抛异常。
"""

from __future__ import annotations

import contextlib
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.core import trace as trace_store

_CURRENT: ContextVar[TraceSession | None] = ContextVar("trace_session", default=None)


def current_trace() -> TraceSession | None:
    return _CURRENT.get()


def _silent(what: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """fail-open：trace 写失败只记文件日志，不阻断业务。"""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.opt(exception=exc).warning("trace 写入失败（{}）", what)
        return None


async def _silent_async(what: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """异步版 fail-open：await 写库失败只记日志。"""
    try:
        return await fn(*args, **kwargs)
    except Exception as exc:
        logger.opt(exception=exc).warning("trace 写入失败（{}）", what)
        return None


@dataclass
class _SpanHandle:
    session: TraceSession
    name: str
    kind: str
    span_id: int | None = None
    started_at: float = 0.0
    input: dict | None = None
    output: dict | None = None
    _closed: bool = False

    def __enter__(self) -> _SpanHandle:
        self.started_at = time.time()
        self.span_id = _silent(
            "add_span",
            trace_store.add_span,
            self.session.run_id,
            self.name,
            self.kind,
            parent_span_id=self.session._top_span(),
            input=self.input,
            started_at=self.started_at,
        )
        self.session._push_span(self.span_id)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.session._pop_span()
        if not self._closed:
            self.finish(
                status="error" if exc is not None else "ok",
                error=exc if isinstance(exc, BaseException) else None,
            )
        return False

    def set_output(self, output: dict | None) -> None:
        self.output = output

    def finish(self, *, status: str = "ok", error: BaseException | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        if self.span_id is not None:
            _silent(
                "finish_span",
                trace_store.finish_span,
                self.span_id,
                status=status,
                output=self.output,
                error=error,
            )


@dataclass
class TraceSession:
    """一次运行（一个用户输入）的 trace 会话。"""

    run_id: str
    question: str
    conversation_id: str = ""
    parent_run_id: str | None = None
    kind: str = "chat"
    _status: str = "running"
    _finished: bool = False
    _stack: list[int] = field(default_factory=list)

    def _push_span(self, span_id: int | None) -> None:
        if span_id is not None:
            self._stack.append(span_id)

    def _pop_span(self) -> None:
        if self._stack:
            self._stack.pop()

    def _top_span(self) -> int | None:
        return self._stack[-1] if self._stack else None

    def span(
        self, name: str, kind: str = "node", *, input: dict | None = None
    ) -> _SpanHandle:
        return _SpanHandle(self, name, kind, input=input)

    def hit(self, *, stage: str, rank: int, **fields: object) -> None:
        _silent(
            "add_hits",
            trace_store.add_hits,
            self.run_id,
            [
                {
                    "stage": stage,
                    "rank": rank,
                    **fields,
                }
            ],
            span_id=self._top_span(),
        )

    def event(
        self, *, level: str = "info", code: str = "", message: str, detail: dict | None = None
    ) -> None:
        _silent(
            "add_event",
            trace_store.add_event,
            self.run_id,
            level=level,
            code=code,
            message=message,
            detail=detail,
        )

    def set_status(self, status: str) -> None:
        """终态前改状态（如 interrupt → waiting）。"""
        self._status = status

    def finish(self, *, status: str | None = None, error: BaseException | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        if error is not None:
            final = "error"
        elif status is not None:
            final = status
        elif self._status == "running":
            final = "ok"  # 正常跑完
        else:
            final = self._status  # waiting / cancelled 等显式状态保持
        _silent(
            "finish_run",
            trace_store.finish_run,
            self.run_id,
            status=final,
            error_kind=getattr(error, "__class__", type(error)).__name__ if error else "",
            error_detail=str(error) if error else "",
        )

    async def finish_async(self, db: Any, *, status: str | None = None, error: BaseException | None = None) -> None:
        """异步收尾（经 AsyncDB 单一异步写者）；与同步 finish 同状态规则。"""
        if self._finished:  # 幂等
            return  # 已收
        self._finished = True  # 标记
        if error is not None:  # 异常
            final = "error"  # 失败
        elif status is not None:  # 显式
            final = status  # 用之
        elif self._status == "running":  # 正常跑完
            final = "ok"  # 成功
        else:  # 显式状态保持
            final = self._status  # 用之
        from app.core.trace import store as _store  # 异步写

        await _silent_async(  # 异步写（fail-open）
            "finish_run",
            _store.finish_run_async,
            db,
            self.run_id,
            status=final,
            error_kind=getattr(error, "__class__", type(error)).__name__ if error else "",
            error_detail=str(error) if error else "",
        )  # 结束


@contextlib.contextmanager
def trace_run(
    *,
    question: str,
    conversation_id: str = "",
    parent_run_id: str | None = None,
    kind: str = "chat",
    run_id: str | None = None,
):
    """一轮运行的生命周期；异常时记 error 并重新抛出。``run_id`` 传入则复用（与发送即落库对齐）。"""
    run_id = run_id or uuid.uuid4().hex
    session = TraceSession(
        run_id=run_id,
        question=question,
        conversation_id=conversation_id,
        parent_run_id=parent_run_id,
        kind=kind,
    )
    _silent(
        "create_run",
        trace_store.create_run,
        run_id,
        question=question,
        conversation_id=conversation_id,
        parent_run_id=parent_run_id,
        kind=kind,
    )
    token = _CURRENT.set(session)
    try:
        yield session
    except BaseException as exc:
        session.finish(error=exc)
        raise
    else:
        session.finish()
    finally:
        _CURRENT.reset(token)


@contextlib.asynccontextmanager
async def trace_run_async(
    db: Any,
    *,
    question: str,
    conversation_id: str = "",
    parent_run_id: str | None = None,
    kind: str = "chat",
    run_id: str | None = None,
):
    """异步版 ``trace_run``：create/finish 走 AsyncDB（单一异步写者）。

    spans/hits/events 仍走同步 store（worker 单 loop 下顺序写，无争用）。
    ``run_id`` 传入则复用（与发送即落库对齐）。
    """
    run_id = run_id or uuid.uuid4().hex  # 运行 id（外部优先）
    session = TraceSession(  # 会话
        run_id=run_id,
        question=question,
        conversation_id=conversation_id,
        parent_run_id=parent_run_id,
        kind=kind,
    )  # 结束
    from app.core.trace import store as _store  # 异步写

    await _silent_async(  # 异步建 run
        "create_run",
        _store.create_run_async,
        db,
        run_id,
        question=question,
        conversation_id=conversation_id,
        parent_run_id=parent_run_id,
        kind=kind,
    )  # 结束
    token = _CURRENT.set(session)  # 绑定当前会话
    try:  # 交给调用方
        yield session  # 返回会话
    except BaseException as exc:  # 异常
        await session.finish_async(db, error=exc)  # 异步收尾
        raise  # 重抛
    else:  # 正常
        await session.finish_async(db)  # 异步收尾
    finally:  # 恢复
        _CURRENT.reset(token)  # 重置


@contextlib.contextmanager
def trace_span(name: str, kind: str = "node", *, input: dict | None = None):
    session = _CURRENT.get()
    if session is None:
        yield _NullSpan()
        return
    with session.span(name, kind=kind, input=input) as span:
        yield span


@dataclass
class _NullSpan:
    """没有活跃 trace 会话时的空操作占位。"""

    def set_output(self, output: dict | None) -> None:  # noqa: ARG002
        return

    def finish(self, *, status: str = "ok", error: BaseException | None = None) -> None:  # noqa: ARG002
        return


def trace_hit(*, stage: str, rank: int, **fields: object) -> None:
    session = _CURRENT.get()
    if session is not None:
        session.hit(stage=stage, rank=rank, **fields)


def trace_event(*, level: str = "info", code: str = "", message: str, detail: dict | None = None) -> None:
    session = _CURRENT.get()
    if session is not None:
        session.event(level=level, code=code, message=message, detail=detail)


def trace_tool(  # 记录一次工具调用的 span
    name: str,  # 工具名
    *,
    ok: bool,  # 是否成功
    code: str,  # 错误码（成功为 "ok"）
    retryable: bool,  # 是否可重试
    cached: bool,  # 是否命中 TTL 缓存（幂等命中）
    args_chars: int,  # 入参体积（字符数）
    output_chars: int,  # 喂给模型的输出体积（字符数）
    error_message: str = "",  # 失败说明（不落原文，仅摘要）
) -> None:
    """记录一次工具调用到 trace（kind=tool），带可记录的全部字段。

    `ms`（耗时）由 span 自身的 started_at→finish 计算（duration 列，不重复传）；
    不落 PII/完整入参/正文，只记体积计数与错误摘要；没有活跃会话则空操作（fail-open）。
    """
    session = _CURRENT.get()  # 当前 trace 会话（无则空操作）
    if session is None:  # 不在 trace 里
        return  # 空操作
    with session.span(name, kind="tool") as span:  # 开工具 span（自动算耗时）
        span.set_output(  # 记录全部可记录字段
            {  # 输出载荷
                "ok": ok,  # 是否成功
                "code": code,  # 错误码
                "retryable": retryable,  # 可重试
                "cached": cached,  # 缓存命中
                "args_chars": args_chars,  # 入参体积
                "output_chars": output_chars,  # 输出体积
                "error_message": (error_message or "")[:300],  # 错误摘要（截断）
            }  # 载荷结束
        )  # 设输出结束
