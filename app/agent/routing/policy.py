"""路由策略灰度：按请求做确定性分桶 → 返回带阈值/版本的策略。

离线可测、不调模型；与 `decide` 的阈值参数一一对应，便于 A/B 滚动。
"""

from __future__ import annotations  # 延迟求值注解

import hashlib  # 稳定性哈希
from dataclasses import dataclass  # 策略
from typing import Sequence  # 序列


@dataclass(frozen=True)
class PolicySpec:  # 一份策略（版本 + 阈值）
    version: str  # 版本（进 RouteDecision.policy_version）
    direct_top: float  # direct 绝对分门槛
    direct_margin: float  # direct 分差门槛
    miss_top: float  # miss 门槛
    display_min: float = 0.25  # 澄清展示下限


#: 当前线上版本（按最初设计：direct 必须 top1≥0.9 且 margin≥0.1；miss=完全无候选）。
DEFAULT_POLICY = PolicySpec("v1", 0.90, 0.10, 0.30, 0.25)  # 稳定版

#: 候选灰度版本（略放宽 direct，观察直接路由是否上升）。
CANDIDATE_POLICY = PolicySpec("v2", 0.88, 0.10, 0.30, 0.25)  # 候选版

#: 灰度比例（候选版流量占比）。
_CANDIDATE_WEIGHT = 0.20  # 20%


def resolve_policy(  # 按请求键确定性分桶
    request_key: str,  # 会话/请求 id
    *,
    candidate: PolicySpec = CANDIDATE_POLICY,  # 候选版
    candidate_weight: float = _CANDIDATE_WEIGHT,  # 候选占比
) -> PolicySpec:  # 命中的策略
    """同一请求键永远同一个版本（可复现）；比例看 candidate_weight。"""
    digest = hashlib.sha256(str(request_key).encode("utf-8")).hexdigest()  # 稳定哈希
    bucket = int(digest[:8], 16) % 1000 / 1000.0  # 0..1
    return candidate if bucket < candidate_weight else DEFAULT_POLICY  # 分桶


def policy_index(policies: Sequence[PolicySpec]) -> dict[str, PolicySpec]:  # 版本 → 策略
    return {policy.version: policy for policy in policies}  # 建索引