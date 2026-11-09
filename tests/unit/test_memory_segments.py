"""M2 分段摘要：追加式存储 + 预算推导 + 滞回触发。"""

from __future__ import annotations  # 延迟值注解

from app.agent.contexts.budget import Budget  # 预算
from app.services import conversations  # 会话（DB）


def test_budget_history_derived_from_window() -> None:  # 预算由 window×比例推导，不写死
    budget = Budget()  # window=1M
    assert budget.window == 1_000_000  # 固定窗口
    expected = int(1_000_000 * budget.history_ratio)  # 按比例
    assert budget.history_budget == expected  # 推导
    assert budget.trim_target == int(expected * 0.8)  # 保留区


def test_budget_should_compact_hysteresis() -> None:  # 滞回与次数上限
    budget = Budget()  # 预算
    assert budget.should_compact(999_999, turns_since_last=5) is True  # 超预算且过滞回
    assert budget.should_compact(999_999, turns_since_last=5, compactions=20) is False  # 次数用尽
    assert budget.should_compact(999_999, turns_since_last=3) is False  # 未过滞回
    assert budget.should_compact(1000, turns_since_last=5) is False  # 未超预算


def _init_schema() -> None:  # 初始化 schema（内存隔离库）
    from app.core.db import schema_v1  # 建表
    from app.core.db.connection import get_connection  # 连接

    schema_v1.initialize(get_connection())  # 初始化


def test_append_segments_are_append_only(fake_root) -> None:  # 追加式
    _init_schema()  # 建表
    conversations.append_segment("c1", start_id=1, end_id=5, summary="s1", source_refs=[1, 2, 3, 4, 5])  # 第一段
    conversations.append_segment("c1", start_id=6, end_id=8, summary="s2", source_refs=[6, 7, 8])  # 第二段
    head = conversations.load_summary("c1")  # 投影头
    assert head["summary"] == "s2" and head["until_message_id"] == 8  # 最后段为准
    segs = conversations.load_segments("c1")  # 全部段
    assert [s["seg_seq"] for s in segs] == [2, 1]  # 追加两段（新在前）
    assert conversations.segment_count("c1") == 2  # 计数


def test_load_summary_empty_without_segments(fake_root) -> None:  # 无段为空
    _init_schema()  # 建表
    head = conversations.load_summary("c1")  # 空会话
    assert head["summary"] == "" and head["until_message_id"] == 0  # 空
    assert conversations.segment_count("c1") == 0  # 0 段