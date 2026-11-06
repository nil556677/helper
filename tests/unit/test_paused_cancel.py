"""暂停态取消：FSM 迁移 + StreamHandle 专门出口。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.context import RunContext  # 上下文
from app.agent.contracts import EventType  # 事件类型
from app.agent.fsm.states import RunEvent, RunState  # 状态
from app.agent.stream import SENTINEL, StreamHandle  # 句柄


def _waiting_ctx(seen: list | None = None) -> RunContext:  # 造 WAITING 上下文
    ctx = RunContext(run_id="r", question="q", emit=(lambda event: (seen.append(event) if seen is not None else None)))  # 上下文
    ctx.transition(RunEvent.START)  # 开始
    ctx.transition(RunEvent.CONTEXT_LOADED)  # 上下文就绪
    ctx.transition(RunEvent.INTERRUPT)  # 暂停
    return ctx  # 返回


def test_fsm_waiting_cancel_path() -> None:  # 暂停→取消
    seen: list = []  # 事件
    ctx = _waiting_ctx(seen)  # 上下文
    assert ctx.machine.state is RunState.WAITING  # 暂停
    ctx.transition(RunEvent.CANCEL_REQUEST)  # 请求取消
    assert ctx.machine.state is RunState.CANCELLING  # 取消中
    ctx.transition(RunEvent.CANCEL_ACK)  # 完成
    assert ctx.machine.state is RunState.CANCELLED  # 已取消


def test_stream_cancel_paused_settles() -> None:  # 句柄专门出口
    seen: list = []  # 事件
    ctx = _waiting_ctx(seen)  # 上下文
    handle = StreamHandle(run_id="r", ctx=ctx)  # 句柄
    handle.paused = True  # 标记暂停
    handle.cancel()  # 取消
    assert ctx.machine.state is RunState.CANCELLED  # 已取消
    assert handle.paused is False  # 不再暂停
    assert handle.events.get_nowait() is SENTINEL  # 关流
    assert seen and seen[-1].type is EventType.CANCELLED  # 发了取消事件