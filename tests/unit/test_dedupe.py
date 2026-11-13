"""M6 无用重试/去重：run_fallback 对同一重复步尝试超限即停，不无限重放。"""

from __future__ import annotations  # 延迟值注解

from app.agent.fallback.run import run_fallback  # 引擎
from app.agent.fallback.schema import Plan, PlanStep, ReplanDecision  # 结构化输出


def _single_planner() -> object:  # 固定一步
    def plan(query, candidates):  # 规划
        del query, candidates  # 不用
        return Plan(steps=[PlanStep(step="永同一步", agent="knowledge_agent")])  # 一步

    return plan  # 返回


def _append_same_replanner() -> object:  # 永远追加同一步
    def replan(state):  # 重规划
        return ReplanDecision(is_done=False, additional_steps=[PlanStep(step="永同一步", agent="knowledge_agent")])  # 追加

    return replan  # 返回


def _counting_runner(calls: list) -> object:  # 计数执行器
    async def run(agent, step, state):  # 执行
        del agent, state  # 不用
        calls.append(1)  # 计数
        return {"ok": True, "result": "ok", "escalate": None, "error": ""}  # 成功

    return run  # 返回


async def test_no_useless_retry() -> None:  # 重复步尝试超限即停
    calls: list = []  # 执行计数
    result = await run_fallback(  # 跑
        {"query": "q", "thread_id": "t"},  # 状态
        planner=_single_planner(),  # 规划
        replanner=_append_same_replanner(),  # 永远追加
        run_expert=_counting_runner(calls),  # 计数
        max_replans=10,  # 大量允许
        max_attempts_per_step=2,  # 单步 2 次上限
    )  # 结束
    assert result["is_done"] is True  # 收敛（止损）
    assert result["error_kind"] == "stop_loss"  # 止损
    assert len(calls) == 2  # 只执行 2 次（不无限重放）
    assert len(result["unsupported_steps"]) == 1  # 重复步被显式拦下（不再重放）


async def test_different_steps_not_limited() -> None:  # 不同步不受单步上限连累
    def two_planner():  # 两步
        def plan(query, candidates):  # 规划
            del query, candidates  # 不用
            return Plan(steps=[PlanStep(step="一", agent="knowledge_agent"), PlanStep(step="二", agent="knowledge_agent")])  # 两步
        return plan  # 返回

    def done_replanner():  # 空则完成
        def replan(state):  # 重规划
            return ReplanDecision(is_done=not bool(state.get("plan") or []), final_response="ok")  # 空则完成
        return replan  # 返回

    calls: list = []  # 计数
    result = await run_fallback(  # 跑
        {"query": "q", "thread_id": "t"},  # 状态
        planner=two_planner(),  # 规划
        replanner=done_replanner(),  # 重规划
        run_expert=_counting_runner(calls),  # 计数
    )  # 结束
    assert result["completion"]["all_done"] is True  # 正常完成
    assert len(calls) == 2  # 两步各执行一次