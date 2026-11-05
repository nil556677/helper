"""兜底子图主循环：规划/执行(含升级)/重规划(含止损)/unsupported。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.fallback.run import run_fallback  # 主循环
from app.agent.fallback.schema import Plan, PlanStep, ReplanDecision  # 结构化输出
from app.agent.fallback.state import FallbackState  # 状态


def _runes(*res: dict) -> object:  # 造 run_expert 假实现
    queue = list(res)  # 队列

    async def runner(agent, step, state):  # 假专家
        del state  # 不用
        return queue.pop(0) if queue else {"ok": True, "result": f"done:{agent}"}  # 取结果

    return runner  # 返回


def _planner(*agents: str) -> object:  # 固定步骤规划器
    def plan(query, candidates):  # 规划
        del query, candidates  # 不用
        return Plan(steps=[PlanStep(step=f"step{i}", agent=agent) for i, agent in enumerate(agents)])  # 计划

    return plan  # 返回


def _replanner(done_after: int = 0) -> object:  # 在第 N 次重规划后完成
    count = {"n": 0}  # 计数

    def replan(state):  # 重规划
        count["n"] += 1  # 计数
        if count["n"] >= done_after:  # 到了
            return ReplanDecision(is_done=True, final_response="答案")  # 完成
        return ReplanDecision(is_done=False, additional_steps=[PlanStep(step="more", agent="query_agent")])  # 追加

    return replan  # 返回


def _state() -> dict:  # 初始状态
    return {"query": "q", "semantic_candidates": [], "thread_id": "t"}  # 基本


async def test_normal_path() -> None:  # 正常：两步 → 完成
    def natural(state):  # 自然完成：计划空才 done
        return ReplanDecision(is_done=not (state.get("plan") or []))  # 空则完成

    result = await run_fallback(
        _state(), planner=_planner("query_agent", "query_agent"),
        replanner=natural, run_expert=_runes({"ok": True, "result": "a"}, {"ok": True, "result": "b"}),
    )
    assert result["is_done"] is True  # 完成
    assert len(result["past_steps"]) == 2  # 两步已执行


async def test_escalate_switches_agent() -> None:  # 领域外 → 切专家
    result = await run_fallback(
        _state(), planner=_planner("query_agent"),
        replanner=_replanner(1),
        run_expert=_runes({"ok": False, "escalate": "action_agent"}, {"ok": True, "result": "e"}),
    )
    assert result["is_done"] is True  # 完成
    assert result["past_steps"][0][1] == "e"  # 升级后的结果


async def test_unsupported_explicit() -> None:  # 无专家显式暴露，不静默
    result = await run_fallback(
        _state(), planner=_planner("unsupported"), replanner=_replanner(1),
        run_expert=_runes({"ok": True, "result": "x"}),
    )
    assert result["is_done"] is True  # 收敛
    assert result["unsupported_steps"]  # 显式记录
    assert result["final_response"]  # 有降级话术（不空）

async def test_stop_loss_forced() -> None:  # 止损
    result = await run_fallback(
        _state(), planner=_planner("query_agent"),
        replanner=_replanner(10),  # 永不完成，一直追加
        run_expert=_runes({"ok": True, "result": "a"}),
        max_replans=2,
    )
    assert result["is_done"] is True  # 强制完成
    assert result["error_kind"] == "stop_loss"  # 止损
    assert result["replan_count"] >= 2  # 计数到位


async def test_failed_expert_becomes_unsupported() -> None:  # 失败 → unsupported，不静默
    result = await run_fallback(
        _state(), planner=_planner("query_agent"),
        replanner=_replanner(1),
        run_expert=_runes({"ok": False, "error": "network"}),
    )
    assert result["is_done"] is True  # 收敛
    assert result["unsupported_steps"]  # 显式失败记录


async def test_denied_step_never_executes() -> None:  # HITL 审批门：拒绝则不执行
    executed: list[str] = []  # 实际执行记录

    async def runner(agent, step, state):  # 记录执行
        del state  # 不用
        executed.append(str(step.get("step")))  # 记录
        return {"ok": True, "result": "x"}  # 成功

    async def deny(agent, step):  # 一律拒绝
        del agent, step  # 不用
        return False  # 拒绝

    result = await run_fallback(
        _state(), planner=_planner("query_agent"),
        replanner=_replanner(1), run_expert=runner, approve=deny,
    )
    assert executed == []  # 一步都没执行
    assert result["unsupported_steps"]  # 被拒步显式标记


async def test_approved_step_executes() -> None:  # HITL 审批门：批准则执行
    executed: list[str] = []  # 记录

    async def runner(agent, step, state):  # 记录
        del state  # 不用
        executed.append(str(step.get("step")))  # 记录
        return {"ok": True, "result": "x"}  # 成功

    async def allow(agent, step):  # 放行
        del agent, step  # 不用
        return True  # 批准

    result = await run_fallback(
        _state(), planner=_planner("query_agent"),
        replanner=_replanner(1), run_expert=runner, approve=allow,
    )
    assert executed == ["step0"]  # 执行了
    assert result["is_done"] is True  # 收敛