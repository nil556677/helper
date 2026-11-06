"""L4a 质量信号与快捷搜索编排。"""

from __future__ import annotations  # 延迟求值注解

from types import SimpleNamespace  # 假运行器

from app.agent.routing.edges import after_dispatch  # 边
from app.agent.routing.fast_search import fast_search  # 编排
from app.agent.routing.quality import assess  # 质量


def _ev(*scores: float, source: str = "资料", text: str = "正文") -> list[dict]:  # 造证据
    return [{"score": s, "source_name": source, "text": text, "origin": f"段{i}"} for i, s in enumerate(scores)]  # 列表


def test_assess_insufficient() -> None:  # 结果太少
    assert assess([], "问题") == (True, "insufficient_results")  # 空
    assert assess(_ev(0.9, 0.9), "问题") == (True, "insufficient_results")  # 少于 3


def test_assess_coverage_low() -> None:  # 平均分低
    assert assess(_ev(0.1, 0.1, 0.1), "问题") == (True, "coverage_low")  # 低分


def test_assess_single_source() -> None:  # 单源但问多源
    assert assess(_ev(0.9, 0.9, 0.9), "A 和 B 对比") == (True, "single_source")  # 单源


def test_assess_open_question() -> None:  # 指向性表述
    evidence = _ev(0.9, 0.9, 0.9)  # 证据
    evidence[0]["text"] = "参见附件 A"  # 指向
    assert assess(evidence, "问题") == (True, "open_question")  # 升级


def test_assess_needs_reasoning() -> None:  # 需推理
    assert assess(_ev(0.9, 0.9, 0.9), "为什么会这样") == (True, "needs_reasoning")  # 升级


def test_assess_pass() -> None:  # 达标
    evidence = _ev(0.9, 0.8, 0.7)  # 三条高分
    evidence[0]["source_name"] = "a"  # 多源
    evidence[1]["source_name"] = "b"  # 多源
    assert assess(evidence, "普通问题") == (False, "")  # 不升级


async def test_fast_search_uses_runner() -> None:  # 编排调用唯一检索源
    class _Runner:  # 假运行器
        async def run(self, query, *, top_k=6, rerank=True):  # 跑
            del query, top_k, rerank  # 不用
            return SimpleNamespace(evidence=_ev(0.9, 0.9, 0.9))  # 固定

    result = await fast_search(_Runner(), "A 和 B 对比", top_k=3)  # 快捷（单源问多源）
    assert result.escalate is True  # 单源 → 升级
    assert result.escalate_reason == "single_source"  # 原因


def test_after_dispatch() -> None:  # 边
    assert after_dispatch({"escalate_reason": "coverage_low"}) == "llm_fallback"  # 升级
    assert after_dispatch({}) == "finalize"  # 收尾