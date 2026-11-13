"""Plan+Execute 编排：接线 planner/replanner/单步ReAct + 独立预算 + 事件，跑 `run_fallback`，
把结果映射回父图契约键（answer/plan_status/completion/last_plan…）。

对外只有一个入口：`run_plan_execute(state, *, model)`（`nodes/plan_execute/node.py` 调用）。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.context import current_run  # 当前 run（发事件）
from app.agent.events import stage as stage_event  # 阶段事件
from app.agent.fallback.budget import PlanBudget  # 独立预算
from app.agent.fallback.factory import (  # 接线
    make_planner,
    make_replanner,
    make_run_expert_single_react,
)
from app.agent.fallback.run import run_fallback  # 引擎

__all__ = ["run_plan_execute"]  # 导出


def _candidates_of(state: dict[str, Any]) -> list[dict[str, Any]]:  # 读语义候选线索
    recall = state.get("semantic_recall") or {}  # 召回
    if not isinstance(recall, dict):  # 非 dict
        return []  # 空
    return list(recall.get("candidates") or [])  # 候选


async def run_plan_execute(  # 主入口
    state: dict[str, Any],  # 父图 State
    *,
    model: Any,  # chat_model
    budget: PlanBudget | None = None,  # 独立预算（缺省新建）
) -> dict[str, Any]:  # 写回父图的更新
    """跑一次 plan+execute，写回 answer/plan_status/completion/last_plan/plan_type 等。"""
    assert model is not None, "plan+execute 需要可用模型"  # 无模型不进来
    budget = budget or PlanBudget()  # 每计划独立新建
    run = current_run()  # 事件出口（可能 None）
    planner = make_planner(model)  # 规划器
    replanner = make_replanner(model)  # 重规划器
    run_expert = make_run_expert_single_react(model)  # 接法 A：单步 ReAct（确认由工具元数据 + 记录状态驱动）

    fstate: dict[str, Any] = {  # 子图状态（与父图隔离契约键）
        "query": str(state.get("question") or state.get("preprocessed") or state.get("question") or ""),  # 问题
        "semantic_candidates": _candidates_of(state),  # 语义候选
        "thread_id": str(state.get("thread_id") or ""),  # 线程
    }  # 结束

    def on_step(out: dict[str, Any]) -> None:  # 每步回调：发计划/步骤事件（无 run 静默）
        _emit_stage(out, run)  # 发事件

    # 用户确认处理器：挂了 checkpoint（hitl_enabled）→ 用 interrupt 内联确认，确认后计划继续剩余步；
    # 未挂（测试/独立调用）→ 交给 run_fallback 的"停止上抛"路径（orchestrator 再上抛 approval_pending）。
    consent_handler = _consent_handler() if bool(state.get("hitl_enabled")) else None  # 处理器

    result = await run_fallback(  # 引擎
        fstate,  # 状态
        planner=planner,  # 规划器
        replanner=replanner,  # 重规划器
        run_expert=run_expert,  # 单步执行
        budget=budget,  # 预算
        on_step=on_step,  # 事件钩子
        consent_handler=consent_handler,  # 确认处理器（内联确认，确认后继续）
    )  # 结束
    # —— 某步需用户确认（告知后执行/记录）：交给路由级确认节点（父图 approval_hitl 弹框）——
    consent = list(result.get("consent_pending") or [])  # 待确认请求
    if consent:  # 有待确认
        return {  # 上抛，不做最终回答（由确认后继续）
            "answer": "",  # 空（等确认后才给）
            "note": "该操作需要您确认后才能执行（已告知将查询/执行什么，确认后留痕）。",  # 说明
            "fragments": [],  # 无引用
            "fallback_used": True,  # 标记
            "plan_type": "plan_execute",  # 执行模式
            "plan_status": result.get("plan_status") or {},  # 计划状态
            "completion": result.get("completion") or {},  # 完成块
            "last_plan": [],  # 计划展示（确认后继续）
            "approval_pending": consent,  # 上抛路由级确认（确认/告知/留痕）
        }  # 结束
    # —— 组装父图答案 ——
    final = str(result.get("final_response") or "").strip()  # 最终回答
    past = list(result.get("past_steps") or [])  # 已执行 (step, result)
    if not final and past:  # 无汇总但有分步结果
        final = "\n".join(str(r).strip() for _s, r in past if str(r).strip())  # 拼结果
    completion = result.get("completion") or {}  # 完成块
    all_done = bool(completion.get("all_done"))  # 是否真正完成
    print_steps = [  # 计划展示
        {"step": str(s.get("step") or ""), "agent": str(s.get("agent") or ""), "status": str(s.get("status") or "")}  # 单步
        for s in (result.get("steps_status") or [])  # 逐步
    ]  # 结束
    note = "已按分步计划完成。" if all_done else "未能完全执行，仅给出可用的部分结果。"  # 说明

    return {  # 写回父图
        "answer": final or "已尝试，但未能完成，请换一种方式描述。",  # 兜底话术
        "note": note,  # 完成/未完成说明
        "fragments": [],  # 引用（单步 ReAct 已带角标，这里不再聚合）
        "fallback_used": True,  # 标记
        "plan_type": "plan_execute",  # 执行模式
        "plan_status": result.get("plan_status") or {},  # 计划状态
        "completion": completion,  # 完成块
        "last_plan": print_steps,  # 计划步骤展示
        "gate": {},  # 门限（单步各自处理）
    }  # 结束


def _emit_stage(out: dict[str, Any], run: Any) -> None:  # 发计划/步骤阶段事件
    """计划生成/每步完成/终止各发一条 STAGE（group=plan）供前端分组展示。"""
    if not run:  # 无出口
        return  # 跳过
    plan_status = out.get("plan_status") or {}  # 状态
    _status = str(plan_status.get("status") or "") if isinstance(plan_status, dict) else ""  # 当前状态
    is_done = bool(out.get("is_done"))  # 是否收敛
    if is_done:  # 终止：发完成事件
        run.emit(stage_event("plan", "done", title="计划执行完成", detail=str(_status), body=_plan_text(out), group="plan"))  # 完成
        return  # 返回
    run.emit(stage_event(  # 步骤（原地更新）
        "plan",  # stage_id 固定
        "running",  # 状态
        title="计划执行中",  # 标题
        detail=f"第 {_done_count(out)}/{_total_count(out)} 步 · {_status}",  # 明细
        body=_plan_text(out),  # 全文
        group="plan",  # 分组
    ))  # 结束


def _done_count(out: dict[str, Any]) -> int:  # 已成功步
    return sum(1 for s in (out.get("steps_status") or []) if s.get("status") == "done")  # 计数


def _consent_handler() -> Any:  # 构造确认处理器（内联 interrupt，确认后继续剩余步）
    """挂在 hitl_enabled（父图有 checkpoint）时用：`interrupt` 暂停问用户 → 确认后 `decide_and_execute`
    真执行并把结果当该步结果，计划继续跑剩余步；拒绝则停该步。与 `approval_hitl_node` 同款弹框。"""
    from app.agent.nodes.approval.node import decide_and_execute  # 幂等决策+执行
    from app.agent.react.graph import CONFIDENTIAL_NOTICE  # 保密提示

    async def handler(consent: list, step: dict[str, Any]) -> tuple[str, str]:  # 处理器
        del step  # 不看步骤文本（纯记录状态驱动）
        first = consent[0] if consent else {}  # 当前一条
        rid = str(first.get("request_id") or "")  # 请求 id
        summary = str(first.get("summary") or "执行该操作")  # 告知内容
        from langgraph.types import interrupt  # 暂停（需父图 checkpoint）

        value = interrupt({  # 弹确认（告知要查/做什么）
            "kind": "approve",  # 类型
            "title": "确认执行",  # 标题
            "description": f"{summary}\n{CONFIDENTIAL_NOTICE}",  # 告知 + 保密提示
            "options": [  # 两个明确选择
                {"value": "approve", "label": "确认执行"},  # 确认
                {"value": "reject", "label": "拒绝"},  # 拒绝
            ],  # 结束
            "request_ids": [rid],  # 请求 id
        })  # 结束
        action = str(value.get("action") if isinstance(value, dict) else value)  # 恢复值
        if action not in ("approve", "reject"):  # 非法/未知
            action = "reject"  # 保守不执行
        out = decide_and_execute(rid, action)  # 幂等决策+执行（approved→查，reject→不查；都留痕）
        if action == "approve" and out.get("executed"):  # 已确认执行
            data = out.get("data")  # 工具出参（含 text）
            text = str(getattr(data, "text", "") or "") if data is not None else ""  # 结果文本
            return ("approved", text or "已确认并执行，已留痕")  # 结果
        return ("rejected", "")  # 拒绝/未执行

    return handler  # 返回


def _total_count(out: dict[str, Any]) -> int:  # 总步数
    return int(out.get("planned_total") or len(out.get("steps_status") or []))  # 总数


def _plan_text(out: dict[str, Any]) -> str:  # 计划步骤文本（事件正文）
    lines: list[str] = []  # 行
    for idx, s in enumerate(out.get("steps_status") or [], start=1):  # 逐步
        mark = "✓" if s.get("status") == "done" else ("•" if s.get("status") == "running" else "·")  # 标记
        lines.append(f"{idx}. {mark} {s.get('step')}")  # 一行
    return "\n".join(lines)  # 返回
