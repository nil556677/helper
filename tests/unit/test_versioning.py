"""M11 配置版本指纹。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.routing.policy import CANDIDATE_POLICY  # 策略
from app.agent.routing.versioning import (  # 被测
    encoder_version,
    fingerprint,
    routes_version,
    thresholds_version,
)


def test_versions_are_present_and_stable() -> None:  # 版本存在且稳定
    assert encoder_version()  # 编码器
    first = routes_version()  # 路由
    assert first and first == routes_version()  # 稳定
    assert thresholds_version() != thresholds_version(CANDIDATE_POLICY)  # 阈值随策略变


def test_fingerprint_shape() -> None:  # 指纹形状
    data = fingerprint()  # 汇总
    assert set(data) == {"encoder_version", "routes_version", "thresholds_version", "config_fingerprint"}  # 四项
    assert all(data.values())  # 都非空
    assert fingerprint() == data  # 可复现