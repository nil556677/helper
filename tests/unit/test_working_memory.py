"""M1 工作记忆白板助手。"""

from __future__ import annotations  # 延迟值注解

from app.agent.memory import (  # 被测
    blank_working,  # 空
    promote_working,  # 收尾
    record_step_working,  # 记步
    set_goal_working,  # 设目标
    touch_working,  # 合并关键事实
    update_working,  # 局部更新
)


def test_blank() -> None:  # 空白板形状
    white = blank_working()  # 空
    assert white["task_goal"] == ""  # 无目标
    assert white["key_facts"] == {}  # 无事实
    assert white["past_steps"] == []  # 无步


def test_set_goal_keeps_facts() -> None:  # 设目标不丢已有 key_facts
    white = touch_working(blank_working(), {"order_id": "37979"})  # 先有事实
    out = set_goal_working(white, "查订单状态")  # 设目标
    assert out["task_goal"] == "查订单状态"  # 目标写入
    assert out["key_facts"]["order_id"] == "37979"  # 事实保留


def test_record_step_archives() -> None:  # 记步归档并清当前步
    white = update_working(blank_working(), current_step="调日程接口")  # 当前步
    out = record_step_working(white, "调日程接口", "200")  # 完成
    assert out["past_steps"] == [("调日程接口", "200")]  # 归档
    assert out["current_step"] == ""  # 当前步已清


def test_touch_merges_single_keys() -> None:  # key_facts 逐键合并（不整体替换）
    white = touch_working(blank_working(), {"amount": 100})  # 先一个
    out = touch_working(white, {"amount": 120, "status": "completed"})  # 覆盖+新增
    assert out["key_facts"]["amount"] == 120  # 覆盖
    assert out["key_facts"]["status"] == "completed"  # 新增


def test_promote_returns_candidates() -> None:  # 收尾取结论候选
    white = touch_working(update_working(blank_working(), current_step="x"), {"id": "A1"})  # 白板
    white = record_step_working(white, "x", "ok")  # 记一步
    steps, facts = promote_working(white)  # 收尾
    assert steps == [("x", "ok")]  # 步骤
    assert facts == {"id": "A1"}  # key_facts


def test_record_step_caps_history() -> None:  # 记步封顶（防白板无界膨胀）
    white = blank_working()  # 空白板
    for i in range(30):  # 记 30 步
        white = update_working(white, current_step=f"s{i}")  # 设当前步
        white = record_step_working(white, f"s{i}", "ok")  # 归档
    assert len(white["past_steps"]) == 20  # 只保留最近 20
    assert white["past_steps"][-1] == ("s29", "ok")  # 最新步在尾部
    assert white["past_steps"][0] == ("s10", "ok")  # 最旧的 10 步被裁剪