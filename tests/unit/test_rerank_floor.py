"""rerank 保底：非空输入绝不压成空表，全低分时保留最高分一条（4b 根因锁死）。"""

from __future__ import annotations

from langchain_core.documents import Document  # 文档

from app.integrations import rerank as rerank_mod  # 目标模块
from app.integrations.rerank import OnnxReranker  # 压缩器


def _docs(n: int = 3) -> list[Document]:  # 造 n 个候选
    return [Document(page_content=f"c{i}") for i in range(n)]  # 列表


def test_all_below_min_keeps_best_one(monkeypatch) -> None:  # 全低分 → 保底一条 → 不再空
    monkeypatch.setattr(rerank_mod, "score_pairs", lambda _q, _d: [-5.0, -6.0, -7.0])  # 全部低于 MIN_SCORE
    out = OnnxReranker(top_n=10).compress_documents(_docs(), "q")  # 压缩
    assert len(out) == 1  # 保底最高分那一条（不是空）
    assert out[0].metadata["rerank_score"] == -5.0  # 带着最高分标签（门限可据此判 weak）

def test_mixed_scores_keeps_above_only(monkeypatch) -> None:  # 混合分 → 只留 >= 闸门的
    monkeypatch.setattr(rerank_mod, "score_pairs", lambda _q, _d: [2.0, -8.0, 1.0])  # 两条过两条不过
    out = OnnxReranker(top_n=10).compress_documents(_docs(), "q")  # 压缩
    assert [d.metadata["rerank_score"] for d in out] == [2.0, 1.0]  # 且降序


def test_skip_when_too_few(monkeypatch) -> None:  # 候选 <2 → 不精排，原样截断
    monkeypatch.setattr(rerank_mod, "score_pairs", lambda _q, _d: [9.0])  # 不会真正调用
    out = OnnxReranker(top_n=10).compress_documents([Document(page_content="only")], "q")  # 1 个候选
    assert len(out) == 1  # 原样返回，不空