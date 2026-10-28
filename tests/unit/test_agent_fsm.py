"""Run 状态机：迁移、非法迁移、取消幂等、启动收敛。"""

from __future__ import annotations  # 类型注解延迟求值

import pytest  # 测试框架

from app.agent.fsm import InvalidTransition, RunEvent, RunMachine, RunState  # 被测对象


def test_happy_path() -> None:  # 正常一轮
    """created → ... → done。"""
    machine = RunMachine()  # 新状态机
    assert machine.state is RunState.CREATED  # 初始态
    assert machine.transition(RunEvent.START) is RunState.CONTEXT_LOADING  # 开始
    assert machine.transition(RunEvent.CONTEXT_LOADED) is RunState.RETRIEVING  # 上下文就绪
    assert machine.transition(RunEvent.RETRIEVAL_DONE) is RunState.GENERATING  # 有证据
    assert machine.transition(RunEvent.GENERATION_DONE) is RunState.FINALIZING  # 生成完
    assert machine.transition(RunEvent.FINALIZED) is RunState.DONE  # 完成
    assert len(machine.history) == 5  # 五次迁移


def test_no_evidence_path() -> None:  # 无证据
    """retrieving 直接到 finalizing。"""
    machine = RunMachine()  # 新状态机
    machine.transition(RunEvent.START)  # 开始
    machine.transition(RunEvent.CONTEXT_LOADED)  # 上下文
    assert machine.transition(RunEvent.NO_EVIDENCE) is RunState.FINALIZING  # 无证据


def test_invalid_transition() -> None:  # 非法迁移
    """不能从 created 直接 generating。"""
    machine = RunMachine()  # 新状态机
    with pytest.raises(InvalidTransition):  # 期望异常
        machine.transition(RunEvent.RETRIEVAL_DONE)  # 跳步


def test_terminal_cancel_is_idempotent() -> None:  # 终态取消
    """done 后再取消不报错。"""
    machine = RunMachine()  # 新状态机
    machine.transition(RunEvent.START)  # 开始
    machine.transition(RunEvent.CONTEXT_LOADED)  # 上下文
    machine.transition(RunEvent.NO_EVIDENCE)  # 无证据
    machine.transition(RunEvent.FINALIZED)  # 完成
    assert machine.state is RunState.DONE  # 终态
    assert machine.transition(RunEvent.CANCEL_REQUEST) is RunState.DONE  # 幂等


def test_cancel_path() -> None:  # 取消路径
    """generating → cancelling → cancelled。"""
    machine = RunMachine()  # 新状态机
    machine.transition(RunEvent.START)  # 开始
    machine.transition(RunEvent.CONTEXT_LOADED)  # 上下文
    machine.transition(RunEvent.RETRIEVAL_DONE)  # 生成
    assert machine.transition(RunEvent.CANCEL_REQUEST) is RunState.CANCELLING  # 请求取消
    assert machine.transition(RunEvent.CANCEL_ACK) is RunState.CANCELLED  # 取消完成


def test_reconcile() -> None:  # 启动收敛
    """活跃态收敛为 interrupted。"""
    machine = RunMachine()  # 新状态机
    machine.transition(RunEvent.START)  # 开始
    assert machine.reconcile() is RunState.INTERRUPTED  # 收敛
    assert machine.reconcile() is RunState.INTERRUPTED  # 终态不变


def test_listener_called() -> None:  # 监听器
    """每次迁移通知一次。"""
    seen: list[str] = []  # 记录
    machine = RunMachine()  # 新状态机
    machine.add_listener(lambda record: seen.append(record.after.value))  # 监听
    machine.transition(RunEvent.START)  # 开始
    assert seen == ["context_loading"]  # 收到
