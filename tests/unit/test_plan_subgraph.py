"""M8 内层子图：正常跑等价；resume_state 剔除已完成步、续跑不重跑。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 异步

from app.agent.fallback.plan_subgraph import build_plan_subgraph, resume_state, run_plan_subgraph  # 子图
from app.agent.fallback.schema import Plan, PlanStep, ReplanDecision  # 结构化输出


def _planner(*texts: str) -> object:  # 固定步骤
    def plan(query, candidates):  # 规划
        del query, candidates  # 不用
        return Plan(steps=[PlanStep(step=t, agent="knowledge_agent") for t in texts])  # 计划

    return plan  # 返回


def _done_replanner() -> object:  # 空则完成
    def replan(state):  # 重规划
        return ReplanDecision(is_done=not bool(state.get("plan") or []), final_response="汇总")  # 空则完成

    return replan  # 返回


def _ok_runner(executed: list) -> object:  # 执行并记录
    async def run(agent, step, state):  # 执行
        del agent, state  # 不用
        executed.append(str(step.get("step")))  # 记录
        return {"ok": True, "result": f"R:{step.get('step')}", "escalate": None, "error": ""}  # 成功

    return run  # 返回


def test_run_plan_subgraph_normal() -> None:  # 子图正常跑完
    executed: list = []  # 记录
    out = asyncio.run(run_plan_subgraph(  # 跑
        {"query": "q", "thread_id": "t"},  # 状态
        planner=_planner("一步", "二步"),  # 规划
        replanner=_done_replanner(),  # 重规划
        run_expert=_ok_runner(executed),  # 执行
    ))  # 结束
    assert executed == ["一步", "二步"]  # 两步执行
    assert out.get("completion", {}).get("all_done") is True  # 完成
    assert out.get("final_response")  # 有终答


def test_subgraph_compiles() -> None:  # 图可编译
    g = build_plan_subgraph(planner=_planner("x"), replanner=_done_replanner(), run_expert=_ok_runner([]))  # 建图
    assert "execute" in g.get_graph().nodes  # 有 execute 节点


def test_resume_state_drops_done_steps() -> None:  # 续跑剔除已完成步
    snapshot = {  # 快照（已完成前两步）
        "steps_status": [
            {"step": "a", "status": "done"},  # 已 done
            {"step": "b", "status": "done"},  # 已 done
            {"step": "c", "status": "pending"},  # 待办
        ],  # 结束
        "plan": [{"step": "a", "agent": "knowledge_agent"}, {"step": "b", "agent": "knowledge_agent"}, {"step": "c", "agent": "knowledge_agent"}],  # 原计划
        "past_steps": [("a", "ra"), ("b", "rb")],  # 已执行结果
        "replan_count": 1,  # 已重规划
        "thread_id": "t",  # 线程
    }  # 结束
    resumed = resume_state(snapshot)  # 续跑（剔除 done）
    assert [s.get("step") for s in resumed["plan"]] == ["c"]  # 只剩待办步
    assert resumed["resume_from"] == 2  # 续跑起点
    assert resumed["past_steps"] == [("a", "ra"), ("b", "rb")]  # 已执行不丢


def test_resume_continues_from_execute(monkeypatch) -> None:  # 从 execute 续跑只跑剩余步
    executed: list = []  # 记录
    snapshot = {  # 快照
        "steps_status": [
            {"step": "a", "status": "done"},  # done
            {"step": "b", "status": "pending"},  # 待办
        ],  # 结束
        "plan": [{"step": "a", "agent": "knowledge_agent"}, {"step": "b", "agent": "knowledge_agent"}],  # 计划
        "past_steps": [("a", "ra")],  # 已执行
        "thread_id": "t",  # 线程
    }  # 结束
    resumed = resume_state(snapshot)  # 剔除 done（plan 只含 b）
    graph = build_plan_subgraph(  # 建从 execute 起跑的子图（跳过 plan 节点）
        planner=_planner("a", "b"),  # 规划（续跑不用）
        replanner=_done_replanner(),  # 重规划
        run_expert=_ok_runner(executed),  # 执行
        start_at="execute",  # 从 execute 起跑
    )  # 结束
    out = asyncio.run(graph.ainvoke(resumed))  # 跑
    assert executed == ["b"]  # 只跑了剩余步 b（不重跑 a）
    assert out.get("completion", {}).get("all_done") is True  # 完成