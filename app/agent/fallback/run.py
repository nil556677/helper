"""兜底子图主循环（Plan-and-Execute + 升级旁路）。

注入三个可调用：`planner` / `replanner` / `run_expert`；保持纯逻辑、可单测。
真实专家/模型通过 `experts.py` 与 `wrapper.py` 接线。
"""

from __future__ import annotations  # 延迟求值注解

from dataclasses import dataclass  # 结果容器
from typing import Any, Awaitable, Callable  # 类型

from app.agent.fallback.schema import Plan, ReplanDecision  # 结构化输出
from app.agent.fallback.state import FallbackState  # 状态

#: 真实专家种类（不含 unsupported 出口标记）。
EXPERT_KINDS: frozenset[str] = frozenset({"knowledge_agent", "general_agent"})  # 专家集合

Planner = Callable[[str, list[dict[str, Any]]], Plan]  # query+candidates → Plan
Replanner = Callable[[FallbackState], ReplanDecision]  # state → ReplanDecision
ExpertRunner = Callable[[str, dict[str, Any], FallbackState], Awaitable[dict[str, Any]]]  # agent+step+state → 结果
Approver = Callable[[str, dict[str, Any]], Awaitable[bool]]  # (agent, step) → 是否批准执行（HITL）


@dataclass(frozen=True)
class ExpertOutcome:  # 专家返回的规范化结果
    ok: bool  # 是否成功
    result: str = ""  # 结果文本
    escalate: str | None = None  # 需要升级到的专家（领域外）
    error: str = ""  # 失败原因


def _empty(state: FallbackState) -> FallbackState:  # 构造空结果
    return dict(state or {})  # 拷贝


async def plan_state(  # Plan 阶段：一次规划产出步骤（规划器同/异步兼容）
    state: FallbackState,  # 子图状态
    planner: Planner,  # 规划器
) -> FallbackState:  # 更新后的状态（async：planner 可能返回协程）
    plan = await _call(planner, state.get("query", ""), list(state.get("semantic_candidates") or []))  # 规划
    out = dict(state)  # 拷贝
    out["plan"] = [step.model_dump(mode="json") for step in plan.steps]  # 步骤（序列化）
    out["replan_count"] = 0  # 计数清零
    out["past_steps"] = []  # 已执行清零
    out["unsupported_steps"] = []  # 无专家清零
    out["is_done"] = False  # 未完成
    out["final_response"] = ""  # 空回答
    out["error_kind"] = ""  # 无终止原因
    return out  # 返回


def _finish(state: FallbackState, response: str = "", *, kind: str = "done") -> FallbackState:  # 收敛
    out = dict(state)  # 拷贝
    out["final_response"] = response or out.get("final_response") or _best(state)  # 最佳回答
    out["is_done"] = True  # 完成
    out["error_kind"] = kind  # 终止原因
    return out  # 返回


def _best(state: FallbackState) -> str:  # 基于已有证据拼最佳回复
    steps = state.get("past_steps") or []  # 已执行
    if not steps:  # 无证据
        return "已尝试，但未能完成，请换一种方式描述。"  # 兜底话术
    return "\n".join(result for _step, result in steps if result)  # 拼结果


async def execute_once(  # Execute 一步（含升级旁路与 unsupported）
    state: FallbackState,  # 状态
    run_expert: ExpertRunner,  # 专家执行
    *,
    approve: Approver | None = None,  # HITL 审批门（对不可逆步）
) -> tuple[FallbackState, dict[str, Any]]:  # (新状态, 该步结论)
    out = dict(state)  # 拷贝
    plan = list(out.get("plan") or [])  # 计划
    if not plan:  # 没步骤
        return out, {}  # 无事
    step = dict(plan[0])  # 当前步
    agent = str(step.get("agent") or "unsupported")  # 归属
    if agent == "unsupported":  # 规划期就标成做不了
        out["unsupported_steps"] = list(out.get("unsupported_steps") or []) + [step]  # 显式暴露
        return _no_step(out), {"kind": "unsupported"}  # 收敛该步
    if agent not in EXPERT_KINDS:  # 非法 agent（防御）
        out["unsupported_steps"] = list(out.get("unsupported_steps") or []) + [step]  # 显式
        return _no_step(out), {"kind": "unsupported"}  # 不静默
    if approve is not None and not await approve(agent, step):  # HITL：未批准
        out["unsupported_steps"] = list(out.get("unsupported_steps") or []) + [step]  # 显式：标记被拒
        return _no_step(out), {"kind": "denied"}  # 不执行
    outcome = ExpertOutcome(**await run_expert(agent, step, state))  # 执行
    if outcome.escalate:  # 领域外 → 切换备选专家重跑（确定性，不触发 Replan）
        target = outcome.escalate  # 目标专家
        if target not in EXPERT_KINDS or target == agent:  # 目标非法
            return _no_step(_unsupported(out, step)), {"kind": "unsupported"}  # 转 unsupported
        outcome = ExpertOutcome(**await run_expert(target, {**step, "agent": target}, state))  # 重跑
    if outcome.ok:  # 成功
        out["past_steps"] = list(out.get("past_steps") or []) + [(str(step.get("step") or ""), outcome.result)]  # 记录
        return _no_step(out), {"kind": "done"}  # 完成该步
    return _no_step(_unsupported(out, step)), {"kind": "failed"}  # 失败 → unsupported


def _no_step(state: FallbackState) -> FallbackState:  # 弹出首步
    out = dict(state)  # 拷贝
    out["plan"] = (out.get("plan") or [])[1:]  # 去掉首步
    return out  # 返回


def _unsupported(state: FallbackState, step: dict[str, Any]) -> FallbackState:  # 记为 unsupported
    out = dict(state)  # 拷贝
    out["unsupported_steps"] = list(out.get("unsupported_steps") or []) + [dict(step)]  # 追加
    return out  # 返回


async def _call(may_be_awaitable: Any, *args: Any, **kwargs: Any) -> Any:  # 兼容同步/异步可调用
    """注入的 planner/replanner 同步或异步皆可；异步才 await。"""
    result = may_be_awaitable(*args, **kwargs)  # 调用
    import inspect  # 判断协程

    return await result if inspect.isawaitable(result) else result  # 异步则 await


async def run_fallback(  # 子图主循环
    state: FallbackState,  # 初始状态
    *,
    planner: Planner,  # 规划器
    replanner: Replanner,  # 重规划器
    run_expert: ExpertRunner,  # 专家执行
    max_replans: int = 2,  # 止损
    approve: Approver | None = None,  # HITL 审批门
) -> FallbackState:  # 最终状态
    out = await plan_state(state, planner)  # Plan（async）
    while not out.get("is_done"):  # 终止前
        out, _ = await execute_once(out, run_expert, approve=approve)  # Execute 一步（含审批/升级/unsupported）
        if out.get("is_done"):  # 已收敛
            break
        decision = await _call(replanner, out)  # Replan 判断（async/同步兼兼容）
        out["replan_count"] = int(out.get("replan_count") or 0) + 1  # 计数
        if decision.is_done:  # 完成
            return _finish(out, response=decision.final_response or "", kind="replan_done")  # 给回答
        if int(out.get("replan_count") or 0) >= max_replans:  # 止损
            return _finish(out, kind="stop_loss")  # 强制收尾
        if decision.additional_steps:  # 追加步骤
            out["plan"] = [s.model_dump(mode="json") for s in decision.additional_steps] + list(out.get("plan") or [])  # 放回
            continue  # 回 Execute
        if not (out.get("plan") or []):  # 计划空且无追加
            return _finish(out, kind="exhausted")  # 不静默结束
    return out  # 返回