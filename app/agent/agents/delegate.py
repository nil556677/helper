"""任务委派：一次委托 = 一个子 run（parent_run_id + trace_runs 记录）。

同机同 worker 下没有网络协议：`task_id` 由本机 TaskManager 生成，状态写 trace_runs，可恢复靠 checkpoint。
"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 并行与超时
import uuid  # 任务 id
from collections.abc import Awaitable, Callable, Sequence  # 类型
from dataclasses import dataclass  # 结果容器
from typing import Any  # 任意值

from app.agent.agents.registry import AgentRegistry  # 注册表
from app.agent.contracts import TaskStatus  # 任务状态
from app.core.trace import store as trace_store  # 运行记录

__all__ = ["DelegationResult", "Executor", "delegate", "delegate_many"]  # 对外导出

#: 执行器：给 (agent_id, task_id)，返回结果。
Executor = Callable[[str, str], Awaitable[Any]]  # 执行函数类型


@dataclass(frozen=True)
class DelegationResult:  # 一次委派结果
    """task_id 与终态都可由看板/对账复用。"""

    task_id: str  # 子 run id
    agent_id: str  # 目标 agent
    status: TaskStatus  # 终态
    output: Any = None  # 结果
    error: str = ""  # 失败原因


async def delegate(  # 委派一次
    registry: AgentRegistry,  # 注册表
    *,
    target: str,  # 目标 agent id
    question: str,  # 任务描述（进 trace question）
    executor: Executor,  # 执行器
    conversation_id: str = "",  # 会话
    parent_run_id: str | None = None,  # 父 run
    timeout_s: float | None = None,  # 超时
    trace_id: str = "",  # 关联追踪
) -> DelegationResult:  # 结果
    """建子 run -> 执行 -> 写终态；任何失败都收敛成终态，不抛给调用方。"""
    registry.get(target)  # 目标必须存在（否则 KeyError）
    task_id = uuid.uuid4().hex  # 本机 TaskManager 生成
    trace_store.create_run(  # 子 run 记录
        task_id,  # run id = task_id
        question=question,  # 任务
        conversation_id=conversation_id,  # 会话
        parent_run_id=parent_run_id,  # 父 run
        kind="agent",  # 委派类型
        metadata={"agent_id": target, "trace_id": trace_id},  # 元数据
    )  # 记录结束
    try:  # 执行
        if timeout_s is None:  # 不限时
            output = await executor(target, task_id)  # 直接跑
        else:  # 限时
            output = await asyncio.wait_for(executor(target, task_id), timeout=timeout_s)  # 超时中断
    except TimeoutError:  # 超时
        trace_store.finish_run(task_id, status="error", error_kind="timeout")  # 记失败
        return DelegationResult(task_id=task_id, agent_id=target, status=TaskStatus.FAILED, error="timeout")  # 失败结果
    except Exception as exc:  # 其他失败
        trace_store.finish_run(task_id, status="error", error_kind=type(exc).__name__)  # 记失败
        return DelegationResult(task_id=task_id, agent_id=target, status=TaskStatus.FAILED, error=type(exc).__name__)  # 失败结果
    trace_store.finish_run(task_id, status="ok")  # 记成功
    return DelegationResult(task_id=task_id, agent_id=target, status=TaskStatus.COMPLETED, output=output)  # 成功结果


async def delegate_many(  # 并行 fan-out
    registry: AgentRegistry,  # 注册表
    targets: Sequence[str],  # 目标列表
    *,
    question: str,  # 任务描述
    executor: Executor,  # 执行器
    conversation_id: str = "",  # 会话
    parent_run_id: str | None = None,  # 父 run
    timeout_s: float | None = None,  # 单任务超时
) -> list[DelegationResult]:  # 结果列表（all-settled）
    """并行跑多路；某一路失败不影响其它路（all-settled）。"""
    calls = [  # 逐路建协程
        delegate(  # 委派
            registry,  # 注册表
            target=target,  # 目标
            question=question,  # 任务
            executor=executor,  # 执行器
            conversation_id=conversation_id,  # 会话
            parent_run_id=parent_run_id,  # 父 run
            timeout_s=timeout_s,  # 超时
        )  # 委派结束
        for target in targets  # 每个目标
    ]  # 列表结束
    return list(await asyncio.gather(*calls))  # 并发等待
