"""取数模式 0 不能被当成缺省值 1（表格行证据曾因此丢失）。"""

from __future__ import annotations  # 类型注解延迟求值

from app.rag.evidence import _fetch_mode, candidate_key  # 被测函数


def test_fetch_mode_zero_is_preserved() -> None:  # 0 保留
    """0/1/2 原样；缺省才是 1。"""
    assert _fetch_mode({"fetch_mode": 0}) == 0  # 独立块/表格行
    assert _fetch_mode({"fetch_mode": 1}) == 1  # 父块
    assert _fetch_mode({"fetch_mode": 2}) == 2  # 宽行
    assert _fetch_mode({}) == 1  # 缺省


def test_mode_zero_candidate_is_chunk() -> None:  # 0 的 key
    """mode=0 按 chunk 去重，不能并到 parent。"""
    key = candidate_key({"fetch_mode": 0, "chunk_id": 4719, "parent_id": 4717})  # key
    assert key == "chunk:4719"  # 独立
