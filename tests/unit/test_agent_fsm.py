"""Run 状态机：迁移、非法迁移、取消幂等、启动收敛。"""

from __future__ import annotations  # 类型注解延迟求值

import ast  # 静态扫描（Q18：节点不得自行 transition）
from pathlib import Path  # 路径

import pytest  # 测试框架

from app.agent.fsm import InvalidTransition, RunEvent, RunMachine, RunState  # 被测对象

_NODES = Path(__file__).resolve().parents[2] / "app" / "agent" / "nodes"  # 节点目录


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


def test_resume_rebuild_seeds_waiting() -> None:  # 跨重启续跑：重建态机的 WAITING 前提（任务7）
    """worker 崩溃后重建 RunContext 时，把全新状态机 seed 到 WAITING 即可恢复续跑。

    迁移表里有 ``(WAITING, RESUME) → GENERATING``，所以重建态机只要让 ``state`` 为 WAITING，
    ``can(RESUME)`` 就成立，driver 的恢复分支（而非误走 START）才能正确执行。
    """
    machine = RunMachine()  # 全新状态机（模拟重启后的重建）
    machine.state = RunState.WAITING  # seed：恢复到"停在 HITL 等用户"的现场
    assert machine.can(RunEvent.RESUME)  # 允恢复
    assert machine.transition(RunEvent.RESUME) is RunState.GENERATING  # 恢复后进入生成


def test_listener_called() -> None:  # 监听器
    """每次迁移通知一次。"""
    seen: list[str] = []  # 记录
    machine = RunMachine()  # 新状态机
    machine.add_listener(lambda record: seen.append(record.after.value))  # 监听
    machine.transition(RunEvent.START)  # 开始
    assert seen == ["context_loading"]  # 收到


def test_nodes_never_call_transition() -> None:  # Q18：节点交权
    """状态机迁移权归 driver：节点目录里禁止出现任何 ``.transition(`` 调用。

    契约收口 Q18（driver 统一迁移）：节点只返回 state update，状态机由 driver 用
    投影表驱动。这里用 AST 静态扫描，防止「顺手在节点里又迁移一次」回潮。
    """
    offenders: list[str] = []  # 违规清单
    for path in sorted(_NODES.rglob("*.py")):  # 逐个节点源文件
        tree = ast.parse(path.read_text(encoding="utf-8"))  # 解析
        for node in ast.walk(tree):  # 遍历所有 AST 节点
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "transition":  # 恰好在调 transition
                rel = str(path.relative_to(Path(__file__).resolve().parents[2])).replace("\\", "/")  # 相对路径
                offenders.append(f"{rel}:{node.lineno}")  # 记录位置
    assert not offenders, f"节点不得自行 transition（迁移权归 driver）：{offenders}"  # 必须为空
