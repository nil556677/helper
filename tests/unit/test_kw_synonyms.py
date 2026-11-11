"""关键词多字段：P4 查询同义词扩展。"""

from __future__ import annotations

from app.core.retrieval.synonyms import expand_tokens  # 扩展


def test_expand_unknown_token_unchanged() -> None:  # 不在组内的词原样
    assert expand_tokens(["订单"]) == ["订单"]  # 无同义词


def test_expand_group_member_bidirectional() -> None:  # 组内任一命中 → 全组展开（双向）
    got = expand_tokens(["退钱"])  # 用同义词组里的词
    assert "退款" in got and "退钱" in got and "退费" in got  # 组内全部出现


def test_expand_dedup_preserves_order() -> None:  # 去重保序
    got = expand_tokens(["退钱", "订单", "退钱"])  # 重复词 + 普通词
    assert got.index("退钱") < got.index("订单")  # 保持原序
    assert got.count("退钱") == 1  # 去重