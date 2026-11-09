"""RunContext：一轮运行的运行时上下文（不进 state / checkpoint）。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 取消事件
import contextlib  # 上下文管理器
import time  # 计时
from collections.abc import Callable, Iterator  # 回调/迭代器
from contextvars import ContextVar  # 当前 run
from dataclasses import dataclass, field  # 数据类
from typing import Any  # 任意值

from app.agent import events  # 事件门面（统一走契约层）
from app.agent.fsm.machine import RunMachine, Transition  # 状态机
from app.agent.fsm.states import RunEvent, RunState  # 状态/事件
from app.agent.metrics import new_metrics  # 指标包


class RunCancelled(RuntimeError):  # 取消异常
    """用户取消 / 客户端断开时抛出，由 runtime 收敛为 cancelled。"""


_CURRENT: ContextVar[RunContext | None] = ContextVar("agent_run_context", default=None)  # 当前 run
#: thread_id → RunContext（driver 图执行期间注册；LangGraph 会剥掉 config 里的非序列化对象）。
_RUNS: dict[str, "RunContext"] = {}


def current_run() -> RunContext | None:  # 取当前 run
    """节点/中间件用它发事件，不直接依赖 runtime。"""
    return _CURRENT.get()  # 读 contextvar


@contextlib.contextmanager
def run_context(ctx: RunContext) -> Iterator[RunContext]:  # 绑定当前 run
    """在 graph/子图执行期间设置 contextvar。"""
    token = _CURRENT.set(ctx)  # 设置
    try:  # 交给调用方
        yield ctx  # 返回上下文
    finally:  # 一定恢复
        _CURRENT.reset(token)  # 重置



def _drop(_event: events.EventEnvelope) -> None:  # 默认事件消费者
    """没有 UI/SSE 消费者时丢弃事件。"""
    return None  # 空操作


@dataclass
class RunContext:  # 一轮运行的运行时上下文
    """不序列化；只活在进程内，随 run 结束销毁。"""

    run_id: str  # 运行 id
    question: str  # 用户问题
    conversation_id: str = ""  # 会话 id
    emit: Callable[[events.EventEnvelope], None] = _drop  # 事件出口
    machine: RunMachine = field(default_factory=RunMachine)  # 状态机
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)  # 取消信号
    db: Any = None  # 本 run 的异步连接（AsyncDB；None=用同步回退，见 load_context）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # 本次运行工具调用汇总（trace 用）
    redactions: list[dict[str, Any]] = field(default_factory=list)  # 脱敏统计
    started_at: float = field(default_factory=time.perf_counter)  # 开始时间
    first_token_at: float | None = None  # 首 token 时间
    partial_answer: str = ""  # 已生成的部分答案（流式中间态；最终进 state.answer）
    persisted: bool = False  # 是否已落库
    steps: list[dict[str, Any]] = field(default_factory=list)  # 过程步骤（stage 累积，落库用）
    context_usage: Any = None  # 本轮上下文占用（contexts.ContextUsage，可观测）
    attachments: list[dict[str, Any]] = field(default_factory=list)  # 本轮附件（输入透传）
    turn_id: str = ""  # 逻辑轮次 id（落库后 = user 消息 id；供 trace/对账关联）
    metrics: dict[str, Any] = field(default_factory=new_metrics)  # 本次 run 指标（可观测，收尾汇总）
    singleflight_base: int = 0  # run 开始时的 single-flight 超时累计值（算 run 内增量）
    interrupt_payload: dict[str, Any] | None = None  # HITL 暂停时的待办负载（澄清/审批）
    step_records: list[dict[str, Any]] = field(default_factory=list)  # 逐节点记录（节点名 + 返回 update，排障）

    def __post_init__(self) -> None:  # 初始化后挂监听
        """状态迁移自动转成 state 事件。"""
        self.machine.add_listener(self._on_transition)  # 注册监听

    def _on_transition(self, record: Transition) -> None:  # 状态监听器
        """把迁移记录转成事件；不在这里写库。"""
        self.emit(  # 发事件
            events.state(  # 状态迁移事件
                record.before.value,  # 迁移前
                record.after.value,  # 迁移后
                record.event.value,  # 触发事件
                record.reason,  # 原因
            )  # 事件结束
        )  # emit 结束

    def transition(self, event: RunEvent, *, reason: str = "") -> RunState:  # 状态迁移
        """委托给 RunMachine；非法迁移直接抛。"""
        return self.machine.transition(event, reason=reason)  # 委托

    def request_cancel(self) -> None:  # 请求取消
        """幂等：终态重复取消不报错。"""
        if self.machine.can(RunEvent.CANCEL_REQUEST):  # 还能迁移
            self.machine.transition(RunEvent.CANCEL_REQUEST, reason="cancel")  # 进入取消
        self.cancel_event.set()  # 唤醒检查点

    def check_cancelled(self) -> None:  # 检查取消
        """中间件/节点在安全点调用。"""
        if self.cancel_event.is_set() or self.machine.state in (  # 已置位或状态是
            RunState.CANCELLING,  # 正在取消
            RunState.CANCELLED,  # 已取消
        ):  # 条件结束
            raise RunCancelled("run cancelled")  # 抛给上层

    def mark_first_token(self) -> None:  # 记录首 token
        """只记第一次，后续 delta 不覆盖。"""
        if self.first_token_at is None:  # 还没有
            self.first_token_at = time.perf_counter()  # 记下

    @property
    def first_token_ms(self) -> int | None:  # 首 token 耗时
        """相对 run 开始。"""
        if self.first_token_at is None:  # 没有首 token
            return None  # 未知
        return int((self.first_token_at - self.started_at) * 1000)  # 毫秒

    @property
    def elapsed_ms(self) -> int:  # 总耗时
        """当前时刻相对开始。"""
        return int((time.perf_counter() - self.started_at) * 1000)  # 毫秒


def ctx_from_config(config: Any) -> RunContext | None:  # 从图配置取上下文
    """按 config 里的 thread_id 找 driver 注册的 run；没有则回退 contextvar。

    LangGraph 的 configurable 必须可序列化，直接塞 RunContext 会被剥掉，
    所以 driver 用 ``thread_id`` 注册进 :data:`_RUNS`，节点再按 thread_id 取回。
    """
    if not config:  # 空配置
        return None  # 没有
    thread_id = str((config.get("configurable") or {}).get("thread_id") or "")  # 线程
    if thread_id:  # 有线程 → 查注册表
        run = _RUNS.get(thread_id)  # 取
        if run is not None:  # 找到
            return run  # 返回
    return current_run()  # 回退 contextvar


def bind_run(thread_id: str, ctx: RunContext) -> None:  # 注册当前 run（driver 图执行前调）
    """执行期间按 thread_id 挂到注册表，供各节点 ``ctx_from_config`` 取回。"""
    _RUNS[thread_id] = ctx  # 写入


def unbind_run(thread_id: str) -> None:  # 执行结束释放
    """图跑完移除，避免残留脏引用。"""
    _RUNS.pop(thread_id, None)  # 删除


def resolve_run(config: Any = None) -> RunContext | None:  # 节点内取 run
    """优先 config 注入的 run_ctx，回退 contextvar；LangGraph 节点执行会丢 caller 的 contextvar。"""
    return ctx_from_config(config) or current_run()  # 先 config 再 contextvar


def bind_node_run(node_fn: Any) -> Any:  # 装饰器：把 run 绑进 contextvar
    """LangGraph 节点在独立上下文执行，``current_run()`` 会读不到 driver 绑定的 run。

    用这个装饰器包住节点：进入时按 ``state.conversation_id``（或 config 的线程）查 driver 注册的
    run，写进 contextvar，节点体内 ``current_run()`` / ``stage()`` 就能发事件了。
    查不到（单测/旧路径）则原样跑。同步/异步节点都兼容。
    """
    import functools  # 包装
    import inspect  # 协程检查

    def _run_of(state: Any, config: Any) -> RunContext | None:  # 按 state/conversation 取 run
        cid = str((state or {}).get("conversation_id") or "")  # 会话 id
        if cid:  # 有会话
            found = _RUNS.get(cid)  # 查注册表
            if found is not None:  # 命中
                return found  # 返回
        return ctx_from_config(config) or current_run()  # 回退 config/contextvar

    if inspect.iscoroutinefunction(node_fn):  # async 节点
        @functools.wraps(node_fn)  # 保留元数据
        async def async_wrapper(state: Any, config: Any = None, **kwargs: Any) -> Any:  # async 包装
            run = _run_of(state, config)  # 取 run
            if run is None:  # 没有（单测直调）
                return await node_fn(state, config, **kwargs)  # 原样
            token = _CURRENT.set(run)  # 设进 contextvar
            try:  # 执行
                return await node_fn(state, config, **kwargs)  # 节点体
            finally:  # 一定恢复
                _CURRENT.reset(token)  # 复位

        return async_wrapper  # 返回 async 包装

    @functools.wraps(node_fn)  # 同步节点
    def sync_wrapper(state: Any, config: Any = None, **kwargs: Any) -> Any:  # 同步包装
        run = _run_of(state, config)  # 取 run
        if run is None:  # 没有
            return node_fn(state, config, **kwargs)  # 原样
        token = _CURRENT.set(run)  # 设进 contextvar
        try:  # 执行
            return node_fn(state, config, **kwargs)  # 节点体
        finally:  # 一定恢复
            _CURRENT.reset(token)  # 复位

    return sync_wrapper  # 返回同步包装
