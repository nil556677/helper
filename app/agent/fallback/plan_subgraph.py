"""内层可 checkpoint 子图（M8）：把 plan→execute→replan 拆成 LangGraph 子图节点。

- 每个 execute 步是节点边界，配合 LangGraph checkpointer 可在"步"粒度 checkpoint（挂 checkpointer
  由调用方决定；本仓库因 AsyncSqliteSaver 不能挂进共享装配（见 checkpoint.py 说明），这里只提供
  子图结构与 resume 契约，调用方在真 worker 单循环里挂）。
- `resume_state(snapshot)`：给定一份计划运行快照（步骤状态/已执行），把"已完成步"从剩余计划里剔除，
  供崩溃后从下一步续跑、**不重跑**已完成的幂等步（见方案 §19）。

对外：`build_plan_subgraph(...)` 返回编译子图 + `run_plan_subgraph(...)` 直接跑 + `resume_state(...)`。
逻辑复用 `fallback/run.py` 的 plan_state / execute_once / `_call`，与主循环等价。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, TypedDict  # 类型

from langgraph.graph import END, START, StateGraph  # 图原语

from app.agent.fallback.budget import PlanBudget  # 独立预算
from app.agent.fallback.run import _call, execute_once, plan_state  # 复用原语（内部强关联）

__all__ = ["build_plan_subgraph", "resume_state", "run_plan_subgraph"]  # 导出


class PlanState(TypedDict, total=False):  # 子图状态（与 FallbackState 对齐；每键一通道）
    query: str  # 问题
    semantic_candidates: list  # 语义候选
    thread_id: str  # 线程
    plan: list  # 剩余计划
    past_steps: list  # 已执行 [(step, result)]
    unsupported_steps: list  # 无专家步骤
    replan_count: int  # 重规划次数
    final_response: str  # 终答
    is_done: bool  # 完成
    error_kind: str  # 终止原因
    steps_status: list  # 单步状态
    attempts: dict  # 去重计数
    planned_total: int  # 计划总步
    plan_status: dict  # 计划状态
    completion: dict  # 完成块
    _kind: str  # 该步结论（路由用）
    resume_from: int  # 续跑起点（观测）


def resume_state(snapshot: dict[str, Any]) -> dict[str, Any]:  # 由快照构造可续跑状态（剔除已完成步）
    """把已 done 的步骤从剩余计划与状态里移除，崩溃后可"从下一步续跑、不重跑已完成的"。

    - ``steps_status`` 保留（供进度展示），只把 done 的步骤从 ``plan`` 弹出；
    - ``past_steps`` 保留（已完成结果），不回退；
    - 返回可直接喂给"从 execute 开始"的续跑（跳过 plan 节点）。
    """
    out = dict(snapshot or {})  # 拷贝
    status_rows = list(out.get("steps_status") or [])  # 步骤状态
    remaining = list(out.get("plan") or [])  # 剩余计划
    # 按顺序跳过前面已 done 的步骤（假设 plan 顺序与 steps_status 顺序一致）
    idx = 0  # 游标
    while idx < len(status_rows) and idx < len(remaining) and status_rows[idx].get("status") == "done":  # 已完成
        idx += 1  # 前进（该步不重跑，从 plan 弹出）
    out["plan"] = remaining[idx:]  # 剩余计划（去掉已完成前缀）
    out["resume_from"] = idx  # 续跑起点（观测）
    out["replan_count"] = int(out.get("replan_count") or 0)  # 保持
    out.setdefault("past_steps", [])  # 保底
    out["is_done"] = False  # 待续跑
    return out  # 返回


def build_plan_subgraph(  # 内层子图：plan → execute → replan → finalize
    *,
    planner: Any,  # 规划器
    replanner: Any,  # 重规划器
    run_expert: Any,  # 专家执行器
    approve: Any | None = None,  # 审批门
    budget: PlanBudget | None = None,  # 独立预算
    max_replans: int = 2,  # 重规划上限
    max_attempts_per_step: int = 2,  # 单步尝试上限
    on_step: Any | None = None,  # 每步回调
    start_at: str = "plan",  # 起始节点（"plan"=正常；"execute"=续跑，跳过规划）
) -> Any:  # 编译子图
    """返回编译后的子图；``start_at="execute"`` 用于 resume_state 后的续跑。"""

    async def n_plan(state: dict[str, Any]) -> dict[str, Any]:  # plan 节点
        return await plan_state(dict(state), planner)  # 规划

    async def n_execute(state: dict[str, Any]) -> dict[str, Any]:  # execute 节点（一步）
        out, okind = await execute_once(  # 执行一步
            dict(state), run_expert, approve=approve, budget=budget, max_attempts_per_step=max_attempts_per_step,  # 参数
        )  # 结束
        if on_step is not None:  # 有钩子
            on_step(out)  # 回调
        kind = str(okind.get("kind") or "") if isinstance(okind, dict) else str(okind)  # 该步结论
        if not out.get("is_done") and kind == "stalled":  # 无用重试被拦
            from app.agent.fallback.run import _finish  # 收敛（止损）

            out = _finish(out, kind="stop_loss")  # 止损
        out = dict(out)  # 拷贝
        out["_kind"] = kind  # 记该步结论（路由用）
        return out  # 返回

    def route_execute(state: dict[str, Any]) -> str:  # execute 后路由
        if state.get("is_done"):  # 已收敛
            return "finalize"  # 收尾
        return "replan"  # 重规划

    async def n_replan(state: dict[str, Any]) -> dict[str, Any]:  # replan 节点
        s = dict(state)  # 拷贝
        decision = await _call(replanner, s)  # 重规划判断
        s["replan_count"] = int(s.get("replan_count") or 0) + 1  # 计数
        if decision.is_done:  # 完成了
            s["is_done"] = True  # 标记
            s["final_response"] = decision.final_response or ""  # 终答
            s["error_kind"] = "replan_done"  # 原因
            return s  # 返回
        if int(s.get("replan_count") or 0) >= max_replans:  # 超限
            s["is_done"] = True  # 标记
            s["error_kind"] = "stop_loss"  # 原因
            return s  # 返回
        if decision.additional_steps:  # 追加步骤
            added = [st.model_dump(mode="json") for st in decision.additional_steps]  # 新步骤
            s["plan"] = added + list(s.get("plan") or [])  # 放回
            return s  # 返回
        if not (s.get("plan") or []):  # 计划空且无追加
            s["is_done"] = True  # 标记
            s["error_kind"] = "exhausted"  # 原因
        return s  # 返回

    def route_replan(state: dict[str, Any]) -> str:  # replan 后路由
        if state.get("is_done"):  # 完成了
            return "finalize"  # 收尾
        return "execute"  # 继续执行

    def n_finalize(state: dict[str, Any]) -> dict[str, Any]:  # finalize 节点（收敛）
        from app.agent.fallback.run import _finish  # 收敛工具

        kind = str(state.get("error_kind") or "done")  # 终止因
        return _finish(dict(state), kind=kind)  # 补最终回答/完成块/计划状态

    g = StateGraph(PlanState)  # 子图状态（每键一通道）
    g.add_node("plan", n_plan)  # 规划
    g.add_node("execute", n_execute)  # 执行
    g.add_node("replan", n_replan)  # 重规划
    g.add_node("finalize", n_finalize)  # 收尾
    g.add_edge(START, start_at)  # 入口（默认 plan；续跑 execute）
    if start_at == "plan":  # 正常路径：规划后接执行
        g.add_edge("plan", "execute")  # plan → execute
    g.add_conditional_edges("execute", route_execute, {"replan": "replan", "finalize": "finalize"})  # 执行分流
    g.add_conditional_edges("replan", route_replan, {"execute": "execute", "finalize": "finalize"})  # 重规划分流
    g.add_edge("finalize", END)  # 结束
    return g.compile()  # 编译（默认无 checkpointer；调用方可换复编译）


async def run_plan_subgraph(  # 直接跑子图（start_at=plan 的正常路径）
    state: dict[str, Any],  # 初始状态（query/thread_id…）
    *,
    planner: Any,  # 规划器
    replanner: Any,  # 重规划器
    run_expert: Any,  # 专家执行器
    approve: Any | None = None,  # 审批门
    budget: PlanBudget | None = None,  # 预算
    max_replans: int = 2,  # 重规划上限
    max_attempts_per_step: int = 2,  # 单步上限
    on_step: Any | None = None,  # 步骤回调
) -> dict[str, Any]:  # 终态
    graph = build_plan_subgraph(  # 建图
        planner=planner,  # 规划器
        replanner=replanner,  # 重规划器
        run_expert=run_expert,  # 执行器
        approve=approve,  # 审批
        budget=budget,  # 预算
        max_replans=max_replans,  # 上限
        max_attempts_per_step=max_attempts_per_step,  # 步上限
        on_step=on_step,  # 回调
    )  # 结束
    return await graph.ainvoke(dict(state))  # 跑到底
