"""计划完成判定 / 推进状态契约：step.status、plan_status、completion。

状态都走可序列化 dict（随 ChatState/checkpoint 持久化），用纯函数构建，保证可单测、可对账。
设计见《执行分层方案》§11/§16：区分"真正完成"与"中途止损"，不把 is_done 一律当成功。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, Literal  # 类型

__all__ = [  # 导出
    "PLAN_STATUSES",  # 计划状态枚举
    "STEP_STATUSES",  # 步骤状态枚举
    "TerminalKind",  # 终止原因类型
    "completion_from_result",  # 由 plan 结果推导完成块
    "make_completion",  # 构建完成块
    "make_plan_status",  # 构建计划状态块
    "new_step",  # 构建单步状态
]

#: 计划生命周期状态。
PLAN_STATUSES = ("planned", "executing", "step_done", "done", "stalled", "stop_loss", "degraded", "failed", "expired")  # 状态

#: 单步状态。
STEP_STATUSES = ("pending", "running", "done", "failed", "denied", "unsupported", "stalled", "awaiting_consent")  # 状态

#: 终止原因种类。
TerminalKind = Literal["done", "replan_done", "stop_loss", "exhausted", "stalled", "failed", "degraded", "expired"]  # 类型


def make_plan_status(  # 构建计划状态块
    status: str = "planned",  # 计划状态（∈PLAN_STATUSES）
    step_index: int = 0,  # 当前第几步（0 起）
    total_steps: int = 0,  # 总步数
    reason: str = "",  # 状态说明/终止原因
) -> dict[str, Any]:  # 状态块
    return {  # 返回
        "status": status,  # 状态
        "step_index": step_index,  # 第几步
        "total_steps": total_steps,  # 总步数
        "reason": reason,  # 说明
    }  # 结束


def new_step(  # 构建单步状态
    step: str,  # 步骤文本（原始）
    agent: str = "",  # 归属专家
    status: str = "pending",  # 单步状态（∈STEP_STATUSES）
    result: str = "",  # 该步结果（成功时）
    error: str = "",  # 失败原因
) -> dict[str, Any]:  # 单步状态
    return {  # 返回
        "step": step,  # 步骤
        "agent": agent,  # 专家
        "status": status,  # 状态
        "result": result,  # 结果
        "error": error,  # 失败
    }  # 结束


def make_completion(  # 构建完成块
    all_done: bool,  # 是否真正完成（步数跑完且有终答）
    finalized: bool,  # 是否有最终回答可交付
    status: str,  # 整体状态（done/stop_loss/…）
    reason: str = "",  # 终止原因
    done_steps: int = 0,  # 已完成步数
    total_steps: int = 0,  # 计划总步数（初始计划）
) -> dict[str, Any]:  # 完成块
    return {  # 返回
        "all_done": all_done,  # 真正完成
        "finalized": finalized,  # 有终答
        "status": status,  # 状态
        "reason": reason,  # 原因
        "done_steps": done_steps,  # 已成功步
        "total_steps": total_steps,  # 总步数（初始）
    }  # 结束


def completion_from_result(  # 由 plan 结果推导完成块（run_fallback 收敛后调用）
    *,
    is_done: bool,  # run_fallback 是否收敛
    final_response: str,  # 最终回答
    error_kind: str,  # 终止原因（run_fallback error_kind）
    done_steps: int,  # 已成功步数
    total_steps: int,  # 计划总步数
    unsupported: bool = False,  # 是否有 unsupported/缺失步
) -> dict[str, Any]:  # 完成块
    """真正完成 = 正常收敛（done/replan_done，非止损/耗尽）且有终答且无缺失步。"""
    normal = error_kind in ("", "done", "replan_done")  # 正常出口
    all_done = bool(  # 全完成
        is_done  # 已收敛
        and normal  # 非止损/耗尽
        and final_response  # 有终答
        and not unsupported  # 无缺失步
    )  # 结束
    if all_done:  # 全完成
        return make_completion(True, True, "done", "plan_complete", done_steps, total_steps)  # 完成
    if is_done and final_response:  # 有终答但不完整（部分完成/止损但有汇总）
        return make_completion(False, True, error_kind or "stop_loss", str(error_kind or "partial"), done_steps, total_steps)  # 部分
    # 无终答/未收敛：明确"未完成"（all_done=False, finalized=False）
    reason = error_kind or "unfinished"  # 终止因
    fail = reason in ("failed", "stalled", "degraded", "expired")  # 是否失败类
    return make_completion(False, False, "failed" if fail else "stop_loss", reason, done_steps, total_steps)  # 未完成
