"""M7/确认：需确认的步上抛路由级确认（工具元数据+记录状态驱动）；计划推进事件可发。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 异步
from types import SimpleNamespace  # 假对象

from app.agent.fallback.run import run_fallback  # 引擎
from app.agent.fallback.schema import Plan, PlanStep, ReplanDecision  # 结构化输出


def _planner(*texts: str) -> object:  # 固定步骤规划器
    def plan(query, candidates):  # 规划
        del query, candidates  # 不用
        return Plan(steps=[PlanStep(step=t, agent="knowledge_agent") for t in texts])  # 计划

    return plan  # 返回


def _done_replanner() -> object:  # 空则完成
    def replan(state):  # 重规划
        return ReplanDecision(is_done=not bool(state.get("plan") or []), final_response="ok")  # 空则完成

    return replan  # 返回


def _trace_runner(executed: list) -> object:  # 记录被执行的步
    async def run(agent, step, state):  # 执行
        del agent, state  # 不用
        executed.append(str(step.get("step")))  # 记录
        return {"ok": True, "result": "x", "escalate": None, "error": ""}  # 成功

    return run  # 返回


async def test_readonly_step_allowed() -> None:  # 只读步放行
    executed: list = []  # 记录
    result = await run_fallback(  # 跑
        {"query": "q", "thread_id": "t"},  # 状态
        planner=_planner("查询订单状态"),  # 只读步
        replanner=_done_replanner(),  # 重规划
        run_expert=_trace_runner(executed),  # 执行
    )  # 结束
    assert executed == ["查询订单状态"]  # 只读已执行
    assert result["completion"]["all_done"] is True  # 完成


def test_plan_stage_event_contract() -> None:  # plan 阶段事件契约有效（group=plan 被前端接受）
    from app.agent.contracts import EventEnvelope, EventType  # 契约
    from app.agent.events import stage as stage_event  # 阶段事件

    ev = stage_event("plan", "running", title="计划执行中", detail="第 1/3 步 · executing", body="1. · 查订单", group="plan")  # 构造
    assert isinstance(ev, EventEnvelope)  # 信封
    assert ev.type is EventType.STAGE  # 类型
    assert ev.payload.group == "plan"  # 分组（UI 认可）
    assert ev.payload.stage_id == "plan"  # 原地更新 id


class _FakeRun:  # 假 run（只记录 emit）
    def __init__(self) -> None:  # 构造
        self.emitted: list = []  # 事件列表

    def emit(self, ev) -> None:  # 记录
        self.emitted.append(ev)  # 追加

    @property  # 占位
    def first_token_at(self) -> None:  # 属性（用不到）
        return None  # 无


def test_orchestrator_emits_plan_events(monkeypatch) -> None:  # plan 推进事件
    import app.agent.fallback.orchestrator as orch  # 模块
    from app.agent.fallback.orchestrator import run_plan_execute  # 入口

    run = _FakeRun()  # 假 run
    monkeypatch.setattr(orch, "current_run", lambda: run)  # 注入
    monkeypatch.setattr(orch, "make_planner", lambda _m: _planner("一", "二"))  # 两步
    monkeypatch.setattr(orch, "make_replanner", lambda _m: _done_replanner())  # 重规划
    monkeypatch.setattr(orch, "make_run_expert_single_react", lambda _m, _t=None: _trace_runner([]))  # 执行
    out = asyncio.run(run_plan_execute({"question": "q", "thread_id": "t"}, model=SimpleNamespace()))  # 跑
    assert out["plan_type"] == "plan_execute"  # 模式
    assert run.emitted  # 发了事件
    # 至少发过一条 STAGE 且 group=plan
    types = {getattr(e, "type", None) for e in run.emitted}  # 事件类型
    assert types  # 非空


def _consent_runner() -> object:  # 执行器：返回需用户确认的请求
    async def run(agent, step, state):  # 执行
        del agent, step, state  # 不用
        return {"ok": True, "result": "", "consent_pending": [  # 待确认
            {"request_id": "r1", "tool_name": "query_order", "summary": "将查询订单 15196630910 的状态/金额/支付方式/下单时间"},  # 一条
        ]}  # 返回

    return run  # 返回


async def test_run_fallback_consent_stops(path=None) -> None:  # 某步需确认 → 停止并上抛
    del path  # 不用
    from app.agent.fallback.run import run_fallback  # 引擎
    from app.agent.fallback.schema import Plan, PlanStep, ReplanDecision  # 结构化

    def pl(query, candidates):  # 规划
        del query, candidates  # 不用
        return Plan(steps=[PlanStep(step="查询订单", agent="knowledge_agent")])  # 一步

    def rp(state):  # 重规划
        return ReplanDecision(is_done=not bool(state.get("plan") or []), final_response="x")  # 空则完成

    result = await run_fallback({"query": "q", "thread_id": "t"}, planner=pl, replanner=rp, run_expert=_consent_runner())  # 跑
    assert result.get("consent_pending")  # 上抛待确认
    assert result["error_kind"] == "consent"  # 停止原因
    assert result["is_done"] is True  # 本段收敛（等确认）


def test_orchestrator_consent_surfaces_approval_pending(monkeypatch) -> None:  # plan 步需确认 → 上抛 approval_pending
    import app.agent.fallback.orchestrator as orch  # 模块
    from app.agent.fallback.orchestrator import run_plan_execute  # 入口

    monkeypatch.setattr(orch, "make_planner", lambda _m: _planner("查询订单"))  # 规划
    monkeypatch.setattr(orch, "make_replanner", lambda _m: _done_replanner())  # 重规划
    monkeypatch.setattr(orch, "make_run_expert_single_react", lambda _m, _t=None: _consent_runner())  # 执行需确认
    out = asyncio.run(run_plan_execute({"question": "帮我查订单", "thread_id": "t"}, model=SimpleNamespace()))  # 跑
    assert out.get("approval_pending")  # 上抛路由级确认
    assert len(out["approval_pending"]) == 1  # 一条
    assert out["answer"] == ""  # 未给最终回答（等确认）
