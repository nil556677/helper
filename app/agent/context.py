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


class RunCancelled(RuntimeError):  # 取消异常
    """用户取消 / 客户端断开时抛出，由 runtime 收敛为 cancelled。"""


_CURRENT: ContextVar[RunContext | None] = ContextVar("agent_run_context", default=None)  # 当前 run


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



def _drop(_event: events.Event) -> None:  # 默认事件消费者
    """没有 UI/SSE 消费者时丢弃事件。"""
    return None  # 空操作


@dataclass
class RunContext:  # 一轮运行的运行时上下文
    """不序列化；只活在进程内，随 run 结束销毁。"""

    run_id: str  # 运行 id
    question: str  # 用户问题
    conversation_id: str = ""  # 会话 id
    emit: Callable[[events.Event], None] = _drop  # 事件出口
    machine: RunMachine = field(default_factory=RunMachine)  # 状态机
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)  # 取消信号
    trace: Any = None  # 追踪 sink（core/trace 适配器）
    tool_counts: dict[str, int] = field(default_factory=dict)  # 工具计数
    redactions: list[dict[str, Any]] = field(default_factory=list)  # 脱敏统计
    started_at: float = field(default_factory=time.perf_counter)  # 开始时间
    first_token_at: float | None = None  # 首 token 时间
    partial_answer: str = ""  # 已生成的部分答案（取消时保留）
    citations: list[dict[str, Any]] = field(default_factory=list)  # 合法引用
    usage: dict[str, Any] = field(default_factory=dict)  # token 用量
    note: str = ""  # 无证据/失败说明
    persisted: bool = False  # 是否已落库
    steps: list[dict[str, Any]] = field(default_factory=list)  # 过程步骤（落库用）
    basis: dict[str, Any] = field(default_factory=dict)  # 检索依据
    gate: dict[str, Any] = field(default_factory=dict)  # 证据门限判决（pass/weak/reject）
    settings: dict[str, Any] = field(default_factory=dict)  # 本轮设置
    reasoning: str = ""  # 模型思考流
    conflict: str = ""  # 资料与通用知识的冲突提示
    attachments: list[dict[str, Any]] = field(default_factory=list)  # 本轮附件
    context_usage: Any = None  # 本轮上下文占用（contexts.ContextUsage）
    turn_id: str = ""  # 逻辑轮次 id（落库后 = user 消息 id；供 trace/对账关联）
    attempt: int = 1  # 第几次尝试（跨轮重试由上层递增）
    status: str = "running"  # 运行状态（running/done/cancelled/failed）

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
    """config 里没有 run_ctx 时返回 None（单测/旧路径）。"""
    if not config:  # 空配置
        return None  # 没有
    return (config.get("configurable") or {}).get("run_ctx")  # 取上下文
