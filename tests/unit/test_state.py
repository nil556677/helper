"""ChatState reducer（E3）：merge_steps 按 id 合并、重放不重复。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.state import merge_steps  # 被测


def test_merge_steps_appends_distinct() -> None:  # 追加
    a = [{"id": "s1", "title": "一"}, {"id": "s2", "title": "二"}]  # 现有
    b = [{"id": "s3", "title": "三"}]  # 更新
    out = merge_steps(a, b)  # 合并
    assert [s["id"] for s in out] == ["s1", "s2", "s3"]  # 顺序保留、不丢


def test_merge_steps_overwrites_same_id_replay_safe() -> None:  # 重放幂等
    a = [{"id": "s1", "status": "running", "title": "x"}]  # 现有（如重放前的旧值）
    b = [{"id": "s1", "status": "done", "title": "x"}]  # 重放返回同一 id
    out = merge_steps(a, b)  # 合并
    assert [s["id"] for s in out] == ["s1"]  # 不重复
    assert out[0]["status"] == "done"  # 新值覆盖


def test_merge_steps_keeps_order_on_mixed() -> None:  # 混合
    a = [{"id": "s1", "title": "一"}, {"id": "s2", "title": "二"}]  # 现有
    b = [{"id": "s2", "title": "二·改"}, {"id": "s4", "title": "四"}]  # 覆盖 + 追加
    out = merge_steps(a, b)  # 合并
    assert [s["id"] for s in out] == ["s1", "s2", "s4"]  # 覆盖保持原位，追加在尾
    assert out[1]["title"] == "二·改"  # 覆盖生效


def test_merge_steps_without_id_appends() -> None:  # 无 id 追加
    out = merge_steps([{"id": "s1"}], [{"no_id": 1}])  # 含无 id 元素
    assert len(out) == 2  # 追加
