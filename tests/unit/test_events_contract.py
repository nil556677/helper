"""事件契约：ContextPayload 接受上下文指标字段（pruned/dropped/cache_prefix）。"""

from __future__ import annotations  # 延迟值注解

from pydantic import ValidationError  # 校验错

from app.agent.events import context  # 上下文事件


def test_context_payload_accepts_context_metrics() -> None:  # 指标字段合法
    env = context(  # 构造（此前 extra_forbid 会炸）
        2,  # turns
        100,  # tokens
        pruned={"recent": 2},  # 剪枝
        dropped={"facts": 3},  # 预算丢
        cache_prefix_tokens=512,  # 缓存前缀
    )  # 结束
    payload = env.payload  # 负载
    assert payload.pruned["recent"] == 2  # 剪枝
    assert payload.dropped["facts"] == 3  # 预算丢
    assert payload.cache_prefix_tokens == 512  # 缓存前缀


def test_context_payload_defaults_ok() -> None:  # 不带指标字段也合法
    env = context(1, 50)  # 最简
    assert env.payload.pruned == {}  # 默认空
    assert env.payload.dropped == {}  # 默认空
    assert env.payload.cache_prefix_tokens == 0  # 默认 0


def test_still_rejects_unknown_extra() -> None:  # 仍拒绝未知字段（防止契约失控）
    import pytest  # 断言异常

    with pytest.raises(ValidationError):  # 期望拒绝
        context(1, 50, bogus_field=1)  # 未知字段