"""多 Agent：注册表、子 run 委派、串行/并行、任务看板投影。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 并行/超时

import pytest  # 测试框架

from app.agent.agents import (  # 被测
    AgentRegistry,
    DuplicateAgentError,
    delegate,
    delegate_many,
    task_board,
)
from app.agent.contracts import AgentCard, TaskStatus  # 契约
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect, get_connection  # 连接


@pytest.fixture(autouse=True)
def _db(fake_root):  # 每个用例独立库
    """建 v1 schema（trace_runs 等）。"""
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关


def _registry() -> AgentRegistry:  # 造注册表
    """两个 agent，其中一个声明能力。"""
    registry = AgentRegistry()  # 新建
    registry.register(AgentCard(agent_id="kb_agent", capabilities=["kb"], tools=["search_kb"]))  # 检索 agent
    registry.register(AgentCard(agent_id="file_agent", capabilities=["file"], tools=["read_file"]))  # 文件 agent
    return registry  # 返回


def test_registry_register_and_query() -> None:  # 注册表
    """按 id / 能力查询。"""
    registry = _registry()  # 注册表
    assert registry.has("kb_agent") is True  # 存在
    assert registry.get("kb_agent").tools == ["search_kb"]  # 能力卡
    assert [c.agent_id for c in registry.by_capability("file")] == ["file_agent"]  # 按能力


def test_registry_duplicate_rejected() -> None:  # 重名
    """同一 agent_id 不能重复。"""
    registry = _registry()  # 注册表
    with pytest.raises(DuplicateAgentError):  # 期望失败
        registry.register(AgentCard(agent_id="kb_agent"))  # 重复


async def test_delegate_success_writes_child_run() -> None:  # 成功委派
    """子 run 落库、终态 ok，结果回传。"""
    async def executor(agent_id: str, task_id: str) -> str:  # 假执行器
        assert agent_id == "kb_agent"  # 目标正确
        return f"done:{task_id}"  # 结果

    result = await delegate(_registry(), target="kb_agent", question="整理资料", executor=executor)  # 委派
    assert result.status is TaskStatus.COMPLETED  # 完成
    assert result.output.startswith("done:")  # 有结果
    row = get_connection().execute("SELECT kind, status, parent_run_id FROM trace_runs WHERE id = ?", (result.task_id,)).fetchone()  # 查库
    assert row["kind"] == "agent"  # 委派类型
    assert row["status"] == "ok"  # 成功


async def test_delegate_failure_is_settled() -> None:  # 失败委派
    """执行器抛错 -> 终态 failed，不抛给调用方。"""
    async def boom(agent_id: str, task_id: str) -> str:  # 抛错
        raise RuntimeError("tool exploded")  # 炸

    result = await delegate(_registry(), target="file_agent", question="读文件", executor=boom)  # 委派
    assert result.status is TaskStatus.FAILED  # 失败
    row = get_connection().execute("SELECT status, error_kind FROM trace_runs WHERE id = ?", (result.task_id,)).fetchone()  # 查库
    assert row["status"] == "error"  # 记失败
    assert row["error_kind"] == "RuntimeError"  # 错误类型


async def test_delegate_timeout() -> None:  # 超时
    """超过时限 -> timeout 终态。"""
    async def slow(agent_id: str, task_id: str) -> str:  # 慢执行器
        await asyncio.sleep(1.0)  # 睡
        return "late"  # 结果

    result = await delegate(_registry(), target="kb_agent", question="慢任务", executor=slow, timeout_s=0.01)  # 委派
    assert result.status is TaskStatus.FAILED  # 失败
    assert result.error == "timeout"  # 原因


async def test_delegate_many_all_settled() -> None:  # 并行
    """一路失败不影响另一路。"""
    async def executor(agent_id: str, task_id: str) -> str:  # 假执行器
        if agent_id == "file_agent":  # 指定一路失败
            raise RuntimeError("no access")  # 炸
        return agent_id  # 成功

    results = await delegate_many(_registry(), ["kb_agent", "file_agent"], question="并行", executor=executor)  # 并行
    statuses = sorted(r.status.value for r in results)  # 终态
    assert statuses == ["completed", "failed"]  # 一路成一败


def test_task_board_projection() -> None:  # 看板
    """看板只读投影能列出委派任务。"""
    async def run() -> None:  # 造两条
        async def executor(agent_id: str, task_id: str) -> str:  # 假执行器
            return agent_id  # 成功

        await delegate(_registry(), target="kb_agent", question="一", executor=executor)  # 第一条
        await delegate(_registry(), target="file_agent", question="二", executor=executor)  # 第二条

    asyncio.run(run())  # 跑
    rows = task_board()  # 看板
    assert len(rows) == 2  # 两条
    assert {r["kind"] for r in rows} == {"agent"}  # 类型
