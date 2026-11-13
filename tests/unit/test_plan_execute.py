"""M2/M4/M5 plan_execute 编排：接线输出 answer/plan_status/completion；图可编译。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 异步跑（同步测试内 await）

from app.agent.fallback.orchestrator import run_plan_execute  # 编排
from app.agent.fallback.schema import Plan, PlanStep, ReplanDecision  # 结构化输出


class _FakeModel:  # 假模型（仅占位，不真调 LLM）
    pass


def _fixed_planner(steps: list[str]) -> object:  # 固定步数规划器
    def plan(query, candidates):  # 规划
        del query, candidates  # 不用
        return Plan(steps=[PlanStep(step=s, agent="knowledge_agent") for s in steps])  # 计划

    return plan  # 返回


def _natural_replanner() -> object:  # 计划空则完成
    def replan(state):  # 重规划
        return ReplanDecision(is_done=not bool(state.get("plan") or []), final_response="汇总答案")  # 空则完成

    return replan  # 返回


def _ok_runner() -> object:  # 每步成功执行器
    async def run(agent, step, state):  # 执行
        del agent, state  # 不用
        return {"ok": True, "result": f"R:{step.get('step')}", "escalate": None, "error": ""}  # 成功

    return run  # 返回


def test_orchestrator_produces_plan_and_completion(monkeypatch) -> None:  # 正常两步 → 完成
    import app.agent.fallback.orchestrator as orch  # 模块（打桩用）

    monkeypatch.setattr(orch, "make_planner", lambda _m: _fixed_planner(["查订单", "查物流"]))  # 规划
    monkeypatch.setattr(orch, "make_replanner", lambda _m: _natural_replanner())  # 重规划
    monkeypatch.setattr(orch, "make_run_expert_single_react", lambda _m, _t=None: _ok_runner())  # 执行
    state = {"question": "请查我订单和发货", "thread_id": "t"}  # 输入
    out = asyncio.run(run_plan_execute(state, model=_FakeModel()))  # 跑
    assert out["plan_type"] == "plan_execute"  # 模式
    assert out["answer"]  # 有答案
    assert out["completion"]["all_done"] is True  # 真正完成
    assert len(out["last_plan"]) == 2  # 两步
    assert out["plan_status"]["status"] == "done"  # 完成状态
    assert out["note"]  # 说明


def test_orchestrator_stop_loss_marks_not_done(monkeypatch) -> None:  # 止损 → 未真正完成
    import app.agent.fallback.orchestrator as orch  # 模块

    def loop_planner(query, candidates):  # 一直追加的规划器
        del query, candidates  # 不用
        return Plan(steps=[PlanStep(step="s", agent="knowledge_agent")])  # 一步

    def loop_replanner(state):  # 永不完成的追加
        return ReplanDecision(is_done=False, additional_steps=[PlanStep(step="s", agent="knowledge_agent")])  # 追加

    monkeypatch.setattr(orch, "make_planner", lambda _m: loop_planner)  # 规划
    monkeypatch.setattr(orch, "make_replanner", lambda _m: loop_replanner)  # 重规划
    monkeypatch.setattr(orch, "make_run_expert_single_react", lambda _m, _t=None: _ok_runner())  # 执行
    out = asyncio.run(run_plan_execute({"question": "q", "thread_id": "t"}, model=_FakeModel()))  # 跑
    assert out["completion"]["all_done"] is False  # 未真正完成
    assert out["note"]  # 未完成说明


def test_route_graph_compiles_with_plan_execute() -> None:  # 父图含 plan_execute 可编译
    from app.agent.agent import build_route_graph  # 父图

    graph = build_route_graph()  # 编译
    assert "plan_execute" in graph.get_graph().nodes  # 节点存在
