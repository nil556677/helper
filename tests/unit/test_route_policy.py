"""灰度：策略版本稳定分桶 + 覆盖率可近视。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.routing.policy import DEFAULT_POLICY, CANDIDATE_POLICY, resolve_policy  # 被测


def test_resolve_policy_is_stable() -> None:  # 同一键同版本
    assert resolve_policy("c0") is resolve_policy("c0")  # 稳定
    assert resolve_policy("a") in (DEFAULT_POLICY, CANDIDATE_POLICY)  # 只能是二者之一


def test_resolve_policy_coverage() -> None:  # 大量键近似覆盖 20%
    candidate_hits = sum(1 for i in range(1000) if resolve_policy(f"k{i}") is CANDIDATE_POLICY)  # 命中数
    ratio = candidate_hits / 1000  # 占比
    assert 0.15 <= ratio <= 0.25  # 约 20%


def test_policy_carries_thresholds() -> None:  # 阈值与版本对应
    assert CANDIDATE_POLICY.direct_top < DEFAULT_POLICY.direct_top  # 候选版更宽
    assert CANDIDATE_POLICY.version != DEFAULT_POLICY.version  # 版本不同
    assert DEFAULT_POLICY.direct_top >= 0.9 and DEFAULT_POLICY.direct_margin >= 0.1  # 原设计 direct 门槛