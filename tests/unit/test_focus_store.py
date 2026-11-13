"""P6 focus_events 审计 + correct_binding（同步、只追加、幂等建表）。"""

from __future__ import annotations  # 延迟值注解

from app.agent.preprocess.focus import store  # 审计仓


def test_record_and_correct_binding(rag_db) -> None:  # 记录 + 纠正（只追加）
    store.record_event(  # 先记一条绑定
        thread_id="t1", turn_index=3, event_type="binding_resolved",  # 基础
        entity_type="order_id", entity_id="12345",  # 绑 12345
        detail={"candidates": [{"id": "12345"}, {"id": "67890"}]},  # 候选
    )  # 结束
    original = store.latest_binding("t1", 3)  # 读原绑定
    assert original and original.get("entity_id") == "12345"  # 原绑定
    out = store.correct_binding("t1", 3, "67890")  # 纠正到 67890
    assert out["original"] == "12345"  # 记下原绑定
    assert out["new_entity_id"] == "67890"  # 新绑定
    # 纠正事件存在，且原事件未被修改（只追加）
    rows = rag_db  # 复用连接（无内容占位）
    del rows  # 不使用
    from app.core.db.connection import get_connection  # DB

    events = get_connection().execute(  # 查该线程事件
        "SELECT event_type, entity_id FROM focus_events WHERE thread_id='t1' AND turn_index=3 ORDER BY id"
    ).fetchall()  # 全部
    types = [r["event_type"] for r in events]  # 类型序列
    assert types == ["binding_resolved", "binding_corrected"]  # 只追加，不修改
    assert events[0]["entity_id"] == "12345"  # 原事件没变


def test_no_thread_skips(rag_db) -> None:  # 无线程不写（静默）
    store.record_event(thread_id="t9", turn_index=1, event_type="entity_added", entity_type="order_id", entity_id="1", )  # 记一条
    out = store.correct_binding("t9_missing", 9, "x")  # 对不存在轮纠正（仍有记录）
    assert out["original"] == ""  # 无原绑定


def test_counts_aggregate(rag_db) -> None:  # 指标聚合（纠正率等）
    store.record_event(thread_id="tx", turn_index=1, event_type="binding_resolved", entity_type="order_id", entity_id="1")  # 绑定
    store.record_event(thread_id="tx", turn_index=2, event_type="binding_ambiguous", entity_type="order_id", entity_id="")  # 歧义
    c = store.counts()  # 全量
    assert c.get("binding_resolved", 0) >= 1  # 有绑定
    assert c.get("binding_ambiguous", 0) >= 1  # 有歧义
