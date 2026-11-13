"""兜底子图主循环（Plan-and-Execute + 升级旁路 + 预算/去重/完成判定）。

注入三个可调用：`planner` / `replanner` / `run_expert`；保持纯逻辑、可单测。
真实专家/模型通过 `experts.py` 与 `wrapper.py` 接线。

`run_fallback` 是本引擎核心；预算/去重/步骤状态/完成判定都是**可选参数**，不传时行为与旧版完全一致
（对既有单测保持兼容）。完整编排（接线 planner/replanner/run_expert + 事件/预算）见
`fallback/orchestrator.py`。
"""

from __future__ import annotations  # 延迟求值注解

from collections.abc import Awaitable, Callable  # 类型
from dataclasses import dataclass  # 结果容器
from typing import Any

from app.agent.contracts.plan_status import (  # 完成/状态
    completion_from_result,
    make_plan_status,
    new_step,
)
from app.agent.fallback.budget import PlanBudget  # 独立预算
from app.agent.fallback.schema import Plan, ReplanDecision  # 结构化输出
from app.agent.fallback.state import FallbackState  # 状态

#: 真实专家种类（不含 unsupported 出口标记）。
EXPERT_KINDS: frozenset[str] = frozenset({"knowledge_agent", "general_agent"})  # 专家集合

Planner = Callable[[str, list[dict[str, Any]]], Plan]  # query+candidates → Plan
Replanner = Callable[[FallbackState], ReplanDecision]  # state → ReplanDecision
ExpertRunner = Callable[[str, dict[str, Any], FallbackState], Awaitable[dict[str, Any]]]  # agent+step+state → 结果
Approver = Callable[[str, dict[str, Any]], Awaitable[bool]]  # (agent, step) → 是否批准执行（HITL）
StepHook = Callable[[dict[str, Any]], None]  # 每步执行后回调（发事件/trace；同步即可）
DedupedHook = Callable[[dict[str, Any]], None]  # 判定为重复步时回调（占位）
ConsentHandler = Callable[[list, dict[str, Any]], Awaitable[tuple[str, str]]]  # (待确认, step) → (决策, 结果或空)

#: 单步最大尝试次数（超限视为无用重试，不再重放该步）。
DEFAULT_MAX_ATTEMPTS_PER_STEP = 2  # 默认


@dataclass(frozen=True)
class ExpertOutcome:  # 专家返回的规范化结果
    ok: bool  # 是否成功
    result: str = ""  # 结果文本
    escalate: str | None = None  # 需要升级到的专家（领域外）
    error: str = ""  # 失败原因
    consent_pending: list | None = None  # 需用户确认的请求（request_id/tool_name/summary；非空即停）


async def plan_state(  # Plan 阶段：一次规划产出步骤（规划器同/异步兼容）
    state: FallbackState,  # 子图状态
    planner: Planner,  # 规划器
) -> FallbackState:  # 更新后的状态（async：planner 可能返回协程）
    plan = await _call(planner, state.get("query", ""), list(state.get("semantic_candidates") or []))  # 规划
    out = dict(state)  # 拷贝
    steps = plan.steps  # 步骤
    out["plan"] = [step.model_dump(mode="json") for step in steps]  # 步骤（序列化）
    out["planned_total"] = len(steps)  # 初始计划总步数
    out["replan_count"] = 0  # 重规划计数清零
    out["past_steps"] = []  # 已执行清零
    out["unsupported_steps"] = []  # 无专家清零
    out["steps_status"] = [new_step(str(s.step), agent=str(s.agent)) for s in steps]  # 单步初始状态
    out["attempts"] = {}  # 步骤去重计数清零
    out["is_done"] = False  # 未完成
    out["final_response"] = ""  # 空回答
    out["error_kind"] = ""  # 无终止原因
    out["plan_status"] = make_plan_status("planned", 0, len(steps))  # 计划状态
    return out  # 返回


def _finish(state: FallbackState, response: str = "", *, kind: str = "done") -> FallbackState:  # 收敛
    out = dict(state)  # 拷贝
    out["final_response"] = response or out.get("final_response") or _best(state)  # 最佳回答
    out["is_done"] = True  # 完成
    out["error_kind"] = kind  # 终止原因
    done_steps = sum(1 for s in out.get("steps_status") or [] if s.get("status") == "done")  # 已成功步
    unsupported = list(out.get("unsupported_steps") or [])  # 无解析步
    total = int(out.get("planned_total") or len(out.get("steps_status") or []))  # 总步数
    out["completion"] = completion_from_result(  # 完成块
        is_done=True,  # 已收敛
        final_response=str(out.get("final_response") or ""),  # 终答
        error_kind=kind,  # 终止因
        done_steps=done_steps,  # 已成功
        total_steps=total,  # 总
        unsupported=bool(unsupported),  # 有缺失
    )  # 结束
    out["plan_status"] = make_plan_status(  # 计划状态
        "done" if kind in ("", "done", "replan_done") else kind,  # 状态
        done_steps,  # 步
        total,  # 总
        reason=kind,  # 原因
    )  # 结束
    return out  # 返回


def _best(state: FallbackState) -> str:  # 基于已有证据拼最佳回复
    steps = state.get("past_steps") or []  # 已执行
    if not steps:  # 无证据
        return "已尝试，但未能完成，请换一种方式描述。"  # 兜底话术
    return "\n".join(result for _step, result in steps if result)  # 拼结果


def _dedupe_key(step: dict[str, Any]) -> str:  # 步骤去重键
    return f"{step.get('step') or ''!s}|{step.get('agent') or ''!s}"  # 文本+专家


def _mark_step_status(  # 更新某一步状态
    out: FallbackState,  # 状态
    index: int,  # 第几步（0 起）
    status: str,  # 单步状态
    result: str = "",  # 结果
    error: str = "",  # 错误
) -> None:  # 就地更新（读写 out["steps_status"]）
    rows = list(out.get("steps_status") or [])  # 行
    if not (0 <= index < len(rows)):  # 越界
        return  # 忽略
    target = dict(rows[index])  # 复制
    target["status"] = status  # 状态
    if result:  # 有结果
        target["result"] = str(result)  # 写
    if error:  # 有错误
        target["error"] = str(error)  # 写
    rows[index] = target  # 写回
    out["steps_status"] = rows  # 存


def _remaining_index(state: FallbackState) -> int:  # 当前要执行的步对应的原始索引
    """由"剩余计划相对初始计划的位置"推算（简化：steps_status 里第一个非终态步的索引）。"""
    rows = list(state.get("steps_status") or [])  # 行
    for i, r in enumerate(rows):  # 逐行
        if r.get("status") in ("pending", "running"):  # 未定/进行中
            return i  # 该索引
    return len(rows)  # 全终结


async def execute_once(  # Execute 一步（含确认/预算/升级/unsupported/去重）
    state: FallbackState,  # 状态
    run_expert: ExpertRunner,  # 专家执行
    *,
    approve: Approver | None = None,  # HITL 审批门（对不可逆步）
    budget: PlanBudget | None = None,  # 独立预算（可选）
    max_attempts_per_step: int = DEFAULT_MAX_ATTEMPTS_PER_STEP,  # 单步尝试上限（去重）
    consent_handler: ConsentHandler | None = None,  # 用户确认处理器（确认后继续剩余步）
) -> tuple[FallbackState, dict[str, Any]]:  # (新状态, 该步结论)
    out = dict(state)  # 拷贝
    plan = list(out.get("plan") or [])  # 计划
    if not plan:  # 没步骤
        return out, {}  # 无事
    step = dict(plan[0])  # 当前步
    agent = str(step.get("agent") or "unsupported")  # 归属
    idx = _remaining_index(out)  # 原始索引
    if agent == "unsupported":  # 规划期就标成做不了
        out["unsupported_steps"] = [*list(out.get("unsupported_steps") or []), step]  # 显式暴露
        _mark_step_status(out, idx, "unsupported", error="no expert planned")  # 状态
        return _no_step(out), {"kind": "unsupported"}  # 收敛该步
    if agent not in EXPERT_KINDS:  # 非法 agent（防御）
        out["unsupported_steps"] = [*list(out.get("unsupported_steps") or []), step]  # 显式
        _mark_step_status(out, idx, "unsupported", error=f"unknown agent {agent}")  # 状态
        return _no_step(out), {"kind": "unsupported"}  # 不静默
    # 去重：同一步尝试超过上限 → 视为无用重试，不再重放
    attempts = dict(out.get("attempts") or {})  # 计数
    key = _dedupe_key(step)  # 键
    if int(attempts.get(key, 0)) >= max_attempts_per_step:  # 已超限
        out["unsupported_steps"] = [*list(out.get("unsupported_steps") or []), step]  # 显式
        _mark_step_status(out, idx, "stalled", error="useless retry")  # 状态
        return _no_step(out), {"kind": "stalled"}  # 该步收敛
    if approve is not None and not await approve(agent, step):  # HITL：未批准
        out["unsupported_steps"] = [*list(out.get("unsupported_steps") or []), step]  # 显式：标记被拒
        _mark_step_status(out, idx, "denied")  # 状态
        return _no_step(out), {"kind": "denied"}  # 不执行
    attempts[key] = int(attempts.get(key, 0)) + 1  # 计数累加
    out["attempts"] = attempts  # 存
    _mark_step_status(out, idx, "running")  # 状态：进行中
    if budget is not None:  # 有预算
        budget.consume_tool()  # 记一次工具调用（近似）
    outcome = ExpertOutcome(**await run_expert(agent, step, state))  # 执行
    if outcome.consent_pending:  # 该步需用户确认（告知后执行/记录）→ 确认后继续剩余步
        consent = list(outcome.consent_pending or [])  # 确认请求列表
        if consent_handler is not None:  # 有确认处理器：确认后当作该步结果，计划继续
            decision, result = await consent_handler(consent, step)  # 用户决策 + 结果
            if decision == "approved":  # 确认执行
                out["past_steps"] = [*list(out.get("past_steps") or []), (str(step.get("step") or ""), result or "已确认并执行")]  # 记为该步
                _mark_step_status(out, idx, "done", result=result or "已确认并执行")  # 状态：完成
                if budget is not None:  # 有预算
                    budget.mark_step()  # 记一步
                return _no_step(out), {"kind": "done"}  # 完成，继续下一步
            # 拒绝 → 显式暴露，不执行该步
            out["unsupported_steps"] = [*list(out.get("unsupported_steps") or []), step]  # 显式
            _mark_step_status(out, idx, "denied", error="user rejected consent")  # 状态：被拒
            return _no_step(out), {"kind": "denied"}  # 不执行该步（继续与否由循环决定）
        # 无内联处理器（测试/未挂 checkpoint）→ 停止计划，交给路由级确认上抛
        out["consent_pending"] = [*list(out.get("consent_pending") or []), *consent]  # 累积
        _mark_step_status(out, idx, "awaiting_consent")  # 状态：等待确认
        return _no_step(out), {"kind": "consent"}  # 停止（不执行该步、不当完成）
    if outcome.escalate:  # 领域外 → 切换备选专家重跑（确定性，不触发 Replan）
        target = outcome.escalate  # 目标专家
        if target not in EXPERT_KINDS or target == agent:  # 目标非法
            _mark_step_status(out, idx, "unsupported", error="bad escalate")  # 状态
            return _no_step(_unsupported(out, step)), {"kind": "unsupported"}  # 转 unsupported
        outcome = ExpertOutcome(**await run_expert(target, {**step, "agent": target}, state))  # 重跑
        if budget is not None:  # 有预算
            budget.consume_tool()  # 记一次
    if outcome.ok:  # 成功
        out["past_steps"] = [*list(out.get("past_steps") or []), (str(step.get("step") or ""), outcome.result)]  # 记录
        _mark_step_status(out, idx, "done", result=outcome.result)  # 状态
        if budget is not None:  # 有预算
            budget.mark_step()  # 记一步
        return _no_step(out), {"kind": "done"}  # 完成该步
    _mark_step_status(out, idx, "failed", error=outcome.error)  # 状态
    return _no_step(_unsupported(out, step)), {"kind": "failed"}  # 失败 → unsupported


def _no_step(state: FallbackState) -> FallbackState:  # 弹出首步
    out = dict(state)  # 拷贝
    out["plan"] = (out.get("plan") or [])[1:]  # 去掉首步
    return out  # 返回


def _unsupported(state: FallbackState, step: dict[str, Any]) -> FallbackState:  # 记为 unsupported
    out = dict(state)  # 拷贝
    out["unsupported_steps"] = [*list(out.get("unsupported_steps") or []), dict(step)]  # 追加
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
    budget: PlanBudget | None = None,  # 独立预算（可选）
    max_attempts_per_step: int = DEFAULT_MAX_ATTEMPTS_PER_STEP,  # 单步尝试上限
    on_step: StepHook | None = None,  # 每步执行后回调（事件/trace）
    on_duplicate: DedupedHook | None = None,  # 重复判定回调（占位）
    consent_handler: ConsentHandler | None = None,  # 用户确认处理器（确认后继续剩余步）
) -> FallbackState:  # 最终状态
    out = await plan_state(state, planner)  # Plan（async）
    if on_step is not None:  # 回调：计划生成
        on_step(out)  # 发事件
    no_progress = 0  # 无进展循环计数
    while not out.get("is_done"):  # 终止前
        out, okind = await execute_once(  # Execute 一步（含确认/审批/升级/unsupported/去重）
            out, run_expert, approve=approve, budget=budget,  # 参数
            max_attempts_per_step=max_attempts_per_step, consent_handler=consent_handler,  # 参数续
        )  # 结束
        if on_step is not None:  # 回调：每步完成
            on_step(out)  # 发事件
        _kind = str(okind.get("kind") or "") if isinstance(okind, dict) else str(okind)  # 该步结论
        if _kind == "consent":  # 该步需用户确认 → 停止计划，交给路由级确认（上抛 approval_pending）
            out["is_done"] = True  # 收敛本段
            out["error_kind"] = "consent"  # 原因
            return out  # 返回
        if _kind == "stalled":  # 无用重试该步被拦
            if on_duplicate is not None:  # 有回调
                on_duplicate(out)  # 触发
            return _finish(out, kind="stop_loss")  # 不再死磕，止损
        if budget is not None and budget.exceeded():  # 预算超限
            return _finish(out, kind=str(budget.exceeded()))  # 止损（带预算 reason）
        if out.get("is_done"):  # 已收敛
            break
        before = _progress_key(out)  # 执行前进度基准
        decision = await _call(replanner, out)  # Replan 判断（async/同步兼兼容）
        out["replan_count"] = int(out.get("replan_count") or 0) + 1  # 计数
        if budget is not None:  # 有预算
            budget.mark_replan()  # 记重规划
        if decision.is_done:  # 完成
            return _finish(out, response=decision.final_response or "", kind="replan_done")  # 给回答
        if int(out.get("replan_count") or 0) >= max_replans:  # 止损
            return _finish(out, kind="stop_loss")  # 强制收尾
        if decision.additional_steps:  # 追加步骤
            added = [s.model_dump(mode="json") for s in decision.additional_steps]  # 新步骤
            out["plan"] = added + list(out.get("plan") or [])  # 放回
            # 若追加的是同一批已尝试过的步骤且无新进度 → 无进展循环
            if _progress_key(out) == before:  # 进度没变
                no_progress += 1  # 计数
                if no_progress >= 2:  # 连续两次无进展
                    return _finish(out, kind="stop_loss")  # 不静默死磕
            else:  # 有进展
                no_progress = 0  # 复位
            continue  # 回 Execute
        if not (out.get("plan") or []):  # 计划空且无追加
            return _finish(out, kind="exhausted")  # 不静默结束
    return out  # 返回


def _progress_key(state: FallbackState) -> tuple:  # 进度指纹（done 步数, 剩余计划步骤串）
    done = sum(1 for s in state.get("steps_status") or [] if s.get("status") == "done")  # 已成功
    remaining = tuple(str(s.get("step")) for s in (state.get("plan") or []))  # 剩余步骤文本
    return (done, remaining)  # 指纹
