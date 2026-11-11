"""full 档增强：置信度门控 + 无增强可用时安全回退基线。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 跑 async

from app.agent.nodes.rag.node import RagResult, RagRunner  # 内核
from app.rag.confidence import confidence_of, should_enhance  # 置信度


def test_confidence_bands() -> None:  # 三态置信度
    assert confidence_of({"decision": "pass", "top1": 0.9}) > confidence_of({"decision": "weak", "top1": 0.5})  # pass 更高
    assert confidence_of({"decision": "reject", "top1": 0.1}) < confidence_of({"decision": "weak", "top1": 0.5})  # reject 更低
    assert 0.0 <= confidence_of(None) <= 1.0  # 空也安全


def test_should_enhance() -> None:  # 低置信才增强；reject/无证据绝不增强
    assert should_enhance({"decision": "reject"}) is False  # reject → 不增强（改写/HyDE 造不出数据）
    assert should_enhance({"decision": "reject", "top1": 0.1}) is False  # 无证据 → 不增强
    assert should_enhance({"decision": "weak", "top1": 0.4}) is True  # 弱但低置信 → 增强
    assert should_enhance({"decision": "pass", "top1": 1.0}) is False  # 高置信 → 不增强


def _runner() -> RagRunner:  # 构造内核实例（不真正跑图）
    return RagRunner(object())  # steps 只在使用时取，构建期不访问


def test_full_no_enhance_when_confident() -> None:  # 高置信不触发
    runner = _runner()  # 内核
    base = RagResult(evidence=[{"text": "a"}], hits=[], errors=[], gate={"decision": "pass", "top1": 0.95})  # 高置信
    out = asyncio.run(runner._enhance("q", base, 6, True, None))  # 跑
    assert out is base  # 原样返回


def test_full_empty_enhance_falls_back() -> None:  # 无变体/无 HyDE → 回退基线
    runner = _runner()  # 内核

    async def _no_variants(_q):  # 无改写
        return []

    async def _no_hyde(_q, _b):  # 无 HyDE
        return []

    runner._variants = _no_variants  # 打桩
    runner._hyde_hits = _no_hyde  # 打桩
    base = RagResult(evidence=[{"text": "a"}], hits=[], errors=[], gate={"decision": "weak", "top1": 0.4})  # 低置信
    out = asyncio.run(runner._enhance("q", base, 6, True, None))  # 跑
    assert out is base  # 回退