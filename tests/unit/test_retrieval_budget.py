"""动态检索预算 + 断崖切分 + 自适应证据。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.routing.retrieval_budget import COMPLEX, plan_for, plan_for_top_k  # 预算
from app.rag.adaptive import cliff_cut  # 断崖


def test_plan_levels() -> None:  # 档位
    assert plan_for("现在几点").level == "simple"  # 简单
    assert plan_for("为什么会这样").level == "complex"  # 复杂标记
    assert plan_for("随便", mode="deep_research").level == "complex"  # 深研
    assert plan_for("随便", path_type="agentic").level == "complex"  # agentic
    assert plan_for("一句不长不短的问题").level in ("simple", "medium")  # 中/简单


def test_plan_for_top_k() -> None:  # 反推
    assert plan_for_top_k(4).level == "simple"  # 少
    assert plan_for_top_k(6).level == "medium"  # 中
    assert plan_for_top_k(20).level == "complex"  # 多


def test_cliff_cut_detects_cliff() -> None:  # 有断崖
    scores = [0.9, 0.88, 0.5, 0.49, 0.48]  # 0.88→0.5 是断崖
    n, reason = cliff_cut(scores, max_n=5, min_n=2, cliff_ratio=2.0, min_cliff=0.05)  # 切
    assert n == 2 and reason == "cliff"  # 断在第二位后


def test_cliff_cut_caps_when_no_cliff() -> None:  # 无断崖走上限
    scores = [0.9, 0.89, 0.88, 0.87, 0.86, 0.85]  # 平滑
    n, reason = cliff_cut(scores, max_n=4, min_n=1)  # 切
    assert n == 4 and reason == "max_cap"  # 上限


def test_cliff_cut_min_floor() -> None:  # 下限保护
    scores = [0.9, 0.1]  # 两个
    n, reason = cliff_cut(scores, max_n=1, min_n=2)  # 上限1 < 下限2
    assert n == 2  # 保下限（不超 len）


def test_evidence_adaptive_uses_cliff(monkeypatch) -> None:  # 证据走断崖（打桩 build_evidence 验证条数）
    import app.rag.retrieval as retrieval_mod  # 模块

    ranked = [{"text": f"c{i}", "metadata": {"rerank_score": s}} for i, s in enumerate([0.9, 0.89, 0.5, 0.49])]  # 2 处断崖
    captured: dict = {}  # 捕获

    def fake_build(docs, *, top_k):  # 打桩
        captured["n"] = top_k  # 记条数
        return [{"index": i} for i in range(top_k)]  # 假结果

    monkeypatch.setattr(retrieval_mod, "build_evidence", fake_build)  # 替换
    retrieval_mod.evidence(ranked, COMPLEX)  # 自适应
    assert captured["n"] == 2  # 断在 0.89→0.5（透传给 build_evidence）
    retrieval_mod.evidence(ranked, 3)  # 固定条数兼容
    assert captured["n"] == 3  # int 直传