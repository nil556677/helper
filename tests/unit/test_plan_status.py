"""M5 完成判定契约：completion_from_result 的正常/部分/未完成三态。"""

from __future__ import annotations  # 延迟值注解

from app.agent.contracts.plan_status import completion_from_result, make_completion, make_plan_status, new_step  # 契约


def test_truly_done_replan() -> None:  # replan_done 正常完成
    c = completion_from_result(is_done=True, final_response="答案", error_kind="replan_done", done_steps=2, total_steps=2)  # 正常
    assert c["all_done"] is True  # 全完成
    assert c["finalized"] is True  # 有终答
    assert c["status"] == "done"  # 状态


def test_truly_done_natural() -> None:  # done 自然完成
    c = completion_from_result(is_done=True, final_response="答案", error_kind="done", done_steps=3, total_steps=3)  # 正常
    assert c["all_done"] is True  # 全完成


def test_stop_loss_partial() -> None:  # 止损但有汇总 → 部分完成（非 all_done）
    c = completion_from_result(is_done=True, final_response="部分结果", error_kind="stop_loss", done_steps=1, total_steps=3)  # 止损
    assert c["all_done"] is False  # 未真正完成
    assert c["finalized"] is True  # 但终答可交付


def test_no_response_unfinished() -> None:  # 无终答 → 未完成
    c = completion_from_result(is_done=False, final_response="", error_kind="failed", done_steps=0, total_steps=2)  # 失败
    assert c["all_done"] is False  # 未完成
    assert c["finalized"] is False  # 无终答
    assert c["status"] == "failed"  # 失败态


def test_unsupported_blocks_done() -> None:  # 有缺失步 → 不算真正完成
    c = completion_from_result(is_done=True, final_response="x", error_kind="replan_done", done_steps=1, total_steps=1, unsupported=True)  # 缺失
    assert c["all_done"] is False  # 不算完成


def test_make_completion_direct() -> None:  # 直接构建
    c = make_completion(True, True, "done", "ok", 2, 2)  # 构建
    assert c["all_done"] and c["status"] == "done"  # 字段


def test_plan_status_block() -> None:  # 计划状态块
    s = make_plan_status("executing", 1, 3, "mid")  # 状态
    assert s["status"] == "executing"  # 状态
    assert s["step_index"] == 1  # 步
    assert s["total_steps"] == 3  # 总


def test_new_step_default() -> None:  # 单步状态默认值
    st = new_step("查一下", agent="knowledge_agent")  # 单步
    assert st["status"] == "pending"  # 待执行
    assert st["agent"] == "knowledge_agent"  # 专家