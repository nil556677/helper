"""Run 状态机：只做迁移，不做业务、不写库、不发 UI 事件。"""

from __future__ import annotations  # 类型注解延迟求值

import threading  # 迁移锁
from collections.abc import Callable  # 监听器类型
from dataclasses import dataclass, field  # 数据类

from loguru import logger  # 监听器异常只告警

from app.agent.fsm.states import TERMINAL_STATES, RunEvent, RunState  # 状态与事件


class InvalidTransition(RuntimeError):  # 非法迁移
    """状态机收到当前状态不允许的事件。"""


#: 迁移表：`(当前状态, 事件) -> 目标状态`；guard 由调用方在发事件前判断。
TRANSITIONS: dict[tuple[RunState, RunEvent], RunState] = {  # 迁移表
    (RunState.CREATED, RunEvent.START): RunState.CONTEXT_LOADING,  # 开始
    (RunState.CONTEXT_LOADING, RunEvent.CONTEXT_LOADED): RunState.RETRIEVING,  # 上下文就绪
    (RunState.CONTEXT_LOADING, RunEvent.FAIL): RunState.FAILED,  # 上下文失败
    (RunState.CONTEXT_LOADING, RunEvent.CANCEL_REQUEST): RunState.CANCELLING,  # 取消
    (RunState.RETRIEVING, RunEvent.RETRIEVAL_DONE): RunState.GENERATING,  # 有证据
    (RunState.RETRIEVING, RunEvent.NO_EVIDENCE): RunState.FINALIZING,  # 无证据
    (RunState.RETRIEVING, RunEvent.FAIL): RunState.FAILED,  # 检索失败
    (RunState.RETRIEVING, RunEvent.CANCEL_REQUEST): RunState.CANCELLING,  # 取消
    (RunState.GENERATING, RunEvent.GENERATION_DONE): RunState.FINALIZING,  # 生成完成
    (RunState.GENERATING, RunEvent.FAIL): RunState.FAILED,  # 生成失败
    (RunState.GENERATING, RunEvent.CANCEL_REQUEST): RunState.CANCELLING,  # 取消
    (RunState.FINALIZING, RunEvent.FINALIZED): RunState.DONE,  # 收尾完成
    (RunState.FINALIZING, RunEvent.FAIL): RunState.FAILED,  # 收尾失败
    (RunState.FINALIZING, RunEvent.CANCEL_REQUEST): RunState.CANCELLING,  # 取消
    # 暂停等待用户（澄清/审批）：任意活跃态可进入 WAITING
    (RunState.CONTEXT_LOADING, RunEvent.INTERRUPT): RunState.WAITING,  # 上下文阶段暂停
    (RunState.RETRIEVING, RunEvent.INTERRUPT): RunState.WAITING,  # 检索阶段暂停
    (RunState.GENERATING, RunEvent.INTERRUPT): RunState.WAITING,  # 生成阶段暂停
    (RunState.FINALIZING, RunEvent.INTERRUPT): RunState.WAITING,  # 收尾阶段暂停
    (RunState.WAITING, RunEvent.RESUME): RunState.GENERATING,  # 补参/选择恢复 → 继续生成
    (RunState.WAITING, RunEvent.CANCEL_REQUEST): RunState.CANCELLING,  # 取消
    (RunState.WAITING, RunEvent.FAIL): RunState.FAILED,  # 等待失败
    (RunState.CANCELLING, RunEvent.CANCEL_ACK): RunState.CANCELLED,  # 取消完成
    (RunState.CANCELLING, RunEvent.FAIL): RunState.FAILED,  # 取消失败
    (RunState.CREATED, RunEvent.CANCEL_REQUEST): RunState.CANCELLED,  # 未开始就取消
}  # 迁移表结束


@dataclass(frozen=True)
class Transition:  # 一次迁移记录
    """供 trace / UI / 测试读取的不可变记录。"""

    before: RunState  # 迁移前
    after: RunState  # 迁移后
    event: RunEvent  # 触发事件
    reason: str = ""  # 人话原因


@dataclass
class RunMachine:  # 一轮运行的状态机
    """纯内存状态机；持久化 / trace / UI 通过监听器外挂。"""

    state: RunState = RunState.CREATED  # 当前状态
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)  # 迁移锁
    _history: list[Transition] = field(default_factory=list, repr=False)  # 迁移历史
    _listeners: list[Callable[[Transition], None]] = field(  # 迁移监听器
        default_factory=list, repr=False
    )  # 监听器列表结束

    def add_listener(self, listener: Callable[[Transition], None]) -> None:  # 注册监听器
        """监听器在锁外调用，抛异常只告警。"""
        self._listeners.append(listener)  # 追加

    @property
    def history(self) -> tuple[Transition, ...]:  # 迁移历史
        """按发生顺序返回迁移记录。"""
        return tuple(self._history)  # 快照

    def can(self, event: RunEvent) -> bool:  # 能否迁移
        """终态的重复取消视为幂等；其余查表。"""
        if self.state in TERMINAL_STATES:  # 已在终态
            return event is RunEvent.CANCEL_REQUEST  # 只有取消是 no-op
        return (self.state, event) in TRANSITIONS  # 查迁移表

    def transition(self, event: RunEvent, *, reason: str = "") -> RunState:  # 执行迁移
        """成功返回新状态；非法迁移抛 :class:`InvalidTransition`。"""
        with self._lock:  # 防并发迁移
            if self.state in TERMINAL_STATES:  # 终态
                if event is RunEvent.CANCEL_REQUEST:  # 重复取消
                    return self.state  # 幂等返回
                raise InvalidTransition(f"终态 {self.state} 不能再迁移（{event}）")  # 拒绝
            target = TRANSITIONS.get((self.state, event))  # 查目标
            if target is None:  # 没有这条边
                raise InvalidTransition(f"{self.state} 不允许事件 {event}")  # 拒绝
            record = Transition(self.state, target, event, reason)  # 造记录
            self.state = target  # 改状态
            self._history.append(record)  # 记历史
        self._notify(record)  # 锁外通知
        return target  # 返回新状态

    def reconcile(self) -> RunState:  # 启动收敛
        """把残留的活跃状态收敛为 INTERRUPTED；终态原样返回。"""
        with self._lock:  # 加锁
            if self.state in TERMINAL_STATES:  # 已是终态
                return self.state  # 不动
            record = Transition(self.state, RunState.INTERRUPTED, RunEvent.RECONCILE, "startup")  # 记录
            self.state = RunState.INTERRUPTED  # 收敛
            self._history.append(record)  # 记历史
        self._notify(record)  # 通知
        return self.state  # 返回

    def _notify(self, record: Transition) -> None:  # 通知监听器
        for listener in list(self._listeners):  # 拷贝后遍历
            try:  # 监听器失败不影响状态机
                listener(record)  # 调用
            except Exception as exc:  # 监听器异常
                logger.opt(exception=exc).warning("状态监听器失败（{}）", record.after)  # 只告警
