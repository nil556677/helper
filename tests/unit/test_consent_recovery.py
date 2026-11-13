"""确认/记录恢复：① approved 但未 executed 的崩溃残留可补跑（幂等）；② 计划里确认后继续剩余步。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 异步

from app.agent.fallback.run import run_fallback  # 引擎
from app.agent.fallback.schema import Plan, PlanStep, ReplanDecision  # 结构化输出


def _planner(*texts: str) -> object:  # 固定步骤
    def plan(query, candidates):  # 规划
        del query, candidates  # 不用
        return Plan(steps=[PlanStep(step=t, agent="knowledge_agent") for t in texts])  # 计划

    return plan  # 返回


def _done_replanner() -> object:  # 空则完成
    def replan(state):  # 重规划
        return ReplanDecision(is_done=not bool(state.get("plan") or []), final_response="ok")  # 空则完成

    return replan  # 返回


def _consent_runner() -> object:  # 执行器：每步都返回"需用户确认"
    async def run(agent, step, state):  # 执行
        del agent, step, state  # 不用
        return {"ok": True, "result": "", "consent_pending": [  # 待确认
            {"request_id": "r1", "tool_name": "query_order", "summary": "将查询订单的状态/金额/支付方式/下单时间"},  # 一条
        ]}  # 返回

    return run  # 返回


async def test_consent_handler_approve_continues_remaining_steps() -> None:  # 确认后继续剩余步
    async def handler(consent, step):  # 模拟用户确认
        del consent, step  # 不用文本
        return ("approved", "订单结果")  # 确认 + 该步结果

    result = await run_fallback(  # 跑
        {"query": "q", "thread_id": "t"},  # 状态
        planner=_planner("查订单", "再查物流"),  # 两步
        replanner=_done_replanner(),  # 重规划
        run_expert=_consent_runner(),  # 都需确认
        consent_handler=handler,  # 内联确认
    )  # 结束
    assert result["is_done"] is True  # 跑完
    assert len(result["past_steps"]) == 2  # 两步都完成（未停在第一步）
    assert result["steps_status"][0]["status"] == "done"  # 第一步完成
    assert result["completion"]["all_done"] is True  # 真正完成


async def test_consent_handler_reject_does_not_run() -> None:  # 拒绝则停该步、不执行
    async def handler(consent, step):  # 用户拒绝
        del consent, step  # 不用
        return ("rejected", "")  # 拒绝

    result = await run_fallback(  # 跑
        {"query": "q", "thread_id": "t"},  # 状态
        planner=_planner("查订单"),  # 一步
        replanner=_done_replanner(),  # 重规划
        run_expert=_consent_runner(),  # 需确认
        consent_handler=handler,  # 拒绝
    )  # 结束
    assert result["steps_status"][0]["status"] == "denied"  # 该步被拒
    assert len(result["unsupported_steps"]) >= 1  # 显式暴露
    assert result["completion"]["all_done"] is False  # 未真正完成


def test_decide_and_execute_recovers_approved_unexecuted(monkeypatch) -> None:  # 确认后崩在 execute 前 → 补跑
    from app.agent.approval import service as approval_service  # 确认服务
    from app.agent.approval.repository import ApprovalConflict  # 冲突
    from app.agent.nodes.approval import node as an  # 确认节点

    repo = an.approval_repo  # 仓储
    monkeypatch.setattr(repo, "decide", lambda *a, **k: (_ for _ in ()).throw(ApprovalConflict()))  # 已处理
    monkeypatch.setattr(repo, "get_request", lambda rid: {"request_id": rid, "status": "approved", "tool_name": "query_order"})  # 已批准
    monkeypatch.setattr(repo, "has_executed", lambda rid: False)  # 未执行

    async def fake_exec(rid, **k):  # 补执行
        del rid  # 不用
        return {"executed": True, "data": None}  # 执行成功

    monkeypatch.setattr(approval_service, "execute_approved", fake_exec)  # 打桩
    out = an.decide_and_execute("r1", "approve")  # 确认方向
    assert out["executed"] is True  # 补执行成功（幂等）


def test_decide_and_execute_already_final_does_not_run(monkeypatch) -> None:  # 已终态（拒/已执行）不补跑
    from app.agent.approval import service as approval_service  # 服务
    from app.agent.approval.repository import ApprovalConflict  # 冲突
    from app.agent.nodes.approval import node as an  # 节点

    repo = an.approval_repo  # 仓储
    monkeypatch.setattr(repo, "decide", lambda *a, **k: (_ for _ in ()).throw(ApprovalConflict()))  # 已处理
    monkeypatch.setattr(repo, "get_request", lambda rid: {"request_id": rid, "status": "rejected", "tool_name": "query_order"})  # 已拒绝
    monkeypatch.setattr(repo, "has_executed", lambda rid: True)  # 已执行（不重复）
    executed: list = []  # 记录

    async def fake_exec(rid, **k):  # 不应被调
        del rid  # 不用
        executed.append(1)  # 记录
        return {"executed": False}  # 无

    monkeypatch.setattr(approval_service, "execute_approved", fake_exec)  # 打桩
    out = an.decide_and_execute("r1", "approve")  # 用户想起确认
    assert out.get("reason") == "already_decided"  # 已终态
    assert executed == []  # 没补跑（拒绝/已执行不重复）


def test_catchup_approved_executes_residual(monkeypatch) -> None:  # 启动补跑已确认未执行
    from app.agent.approval import repository as repo  # 仓储
    from app.agent.approval import service as approval_service  # 服务

    monkeypatch.setattr(repo, "list_approved_unexecuted", lambda limit=50: [  # 一条残留
        {"request_id": "r1", "tool_name": "query_order", "resource_summary": "订单"}  # 记录
    ])  # 打桩

    async def fake_exec(rid, **k):  # 补执行
        del rid  # 不用
        return {"executed": True, "data": None}  # 成功

    monkeypatch.setattr(approval_service, "execute_approved", fake_exec)  # 打桩
    done = asyncio.run(approval_service.catchup_approved())  # 补跑
    assert done == ["r1"]  # 真正补跑了一条
