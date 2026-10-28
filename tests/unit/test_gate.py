"""M2 证据门限：硬要素、三态判决、QA 方向性、以及"明知拦不住"的边界用例。"""

from __future__ import annotations  # 类型注解延迟求值

from app.rag import gate as gate_mod  # 门限
from app.rag import hard  # 硬要素


def _hit(score: float | None, snippet: str = "违约金按合同约定计算。") -> dict:  # 造一条命中
    """score=None 表示没走精排（候选太少）。"""
    meta = {"snippet": snippet, "score": 0.02, "score_kind": "rrf"}  # metadata
    if score is not None:  # 有精排分
        meta["rerank_score"] = score  # 记上
    return {"text": snippet, "metadata": meta}  # 命中


def _high(_question: str, snippets: list[str]) -> list[float]:  # 高方向性
    """假装每条都很切题。"""
    return [3.0 for _ in snippets]  # 高分


def _low(_question: str, snippets: list[str]) -> list[float]:  # 低方向性
    """假装答非所问。"""
    return [-2.0 for _ in snippets]  # 低分


# ---------------- 硬要素 ----------------

def test_hard_extracts_clause_amount_date() -> None:  # 抽取
    """三类要素都能抽出来并归一化。"""
    found = hard.extract("第 3.2 条约定 30 万，2024 年 3 月 15 日签")  # 抽取
    assert "3.2" in found["clause"]  # 条款号
    assert "300000" in found["amount"]  # 金额归一成元
    assert "2024-03-15" in found["date"]  # 日期


def test_hard_missing_marks_clause_as_hard() -> None:  # 反
    """条款号没命中 → 硬缺。"""
    hard_missing, soft_missing = hard.missing_elements("第 9.9 条怎么写", ["第 3.2 条……"])  # 缺什么
    assert hard_missing == ["9.9"]  # 硬缺
    assert soft_missing == []  # 没金额


def test_hard_amount_is_soft() -> None:  # 反（软缺）
    """金额没命中只算软缺（可能写成中文数字）。"""
    hard_missing, soft_missing = hard.missing_elements("违约金 30 万", ["违约金按合同约定"])  # 缺什么
    assert hard_missing == []  # 不算硬缺
    assert soft_missing == ["300000"]  # 软缺


# ---------------- 三态 ----------------

def test_gate_pass() -> None:  # G-01 正
    """硬要素不缺、分高、方向性高 → PASS。"""
    decision = gate_mod.gate_hits("违约金怎么算", [_hit(2.0), _hit(1.6)], qa_scorer=_high)  # 判决
    assert decision.decision == gate_mod.PASS  # 通过
    assert decision.top1 == 2.0  # 最高分
    assert len(decision.kept) == 2  # 都留下


def test_gate_weak_on_low_score() -> None:  # G-02 正（弱）
    """分数在灰区 → WEAK，仍可答。"""
    decision = gate_mod.gate_hits("违约金怎么算", [_hit(0.5), _hit(0.3)], qa_scorer=_high)  # 判决
    assert decision.decision == gate_mod.WEAK  # 弱
    assert "score_weak" in decision.reasons  # 原因
    assert decision.kept  # 没被丢掉


def test_gate_reject_without_evidence() -> None:  # G-03 反
    """一条都没有 → REJECT。"""
    decision = gate_mod.gate_hits("违约金怎么算", [], qa_scorer=_high)  # 判决
    assert decision.decision == gate_mod.REJECT  # 拒
    assert decision.kept == []  # 无证据


def test_gate_reject_on_hard_missing_even_with_high_score() -> None:  # G-04 反
    """编号没命中时必须拒，哪怕分数很高。"""
    decision = gate_mod.gate_hits("第 9.9 条怎么写", [_hit(6.0, "第 3.2 条……")], qa_scorer=_high)  # 判决
    assert decision.decision == gate_mod.REJECT  # 拒
    assert decision.hard_missing == ["9.9"]  # 报出缺什么
    assert "hard_missing" in decision.reasons  # 原因


def test_gate_reject_on_too_few() -> None:  # G-05 反
    """只有一条证据 → 不足以支撑回答。"""
    decision = gate_mod.gate_hits("违约金怎么算", [_hit(3.0)], qa_scorer=_high)  # 判决
    assert decision.decision == gate_mod.REJECT  # 拒
    assert "too_few" in decision.reasons  # 原因


def test_gate_reject_when_all_below_threshold() -> None:  # G-06 反
    """全部低于拒答线 → 粗筛后为空。"""
    low = gate_mod.TOP1_REJECT - 1.0  # 低于线
    decision = gate_mod.gate_hits("违约金怎么算", [_hit(low), _hit(low)], qa_scorer=_high)  # 判决
    assert decision.decision == gate_mod.REJECT  # 拒
    assert decision.kept == []  # 全丢


def test_gate_reject_on_qa_low() -> None:  # G-09 反（方向性）
    """精排分高但 QA 判"答非所问" → 拒。"""
    decision = gate_mod.gate_hits("违约金上限是多少", [_hit(4.0, "违约金按日万分之五计算。"), _hit(3.8)], qa_scorer=_low)  # 判决
    assert decision.decision == gate_mod.REJECT  # 拒
    assert "qa_low" in decision.reasons  # 原因
    assert decision.kept == []  # 不再往下走


def test_gate_cannot_catch_subject_mismatch() -> None:  # G-10 反（能力边界）
    """主体错位（甲方/乙方）reranker 分不出来 → 门限会放行，这条测试把边界钉住。"""
    decision = gate_mod.gate_hits("甲方有什么义务", [_hit(5.0, "乙方应按期支付价款。"), _hit(4.0)], qa_scorer=_high)  # 判决
    assert decision.decision == gate_mod.PASS  # 门限确实放行了
    assert "qa_low" not in decision.reasons  # 不是靠方向性拦住的


def test_gate_works_without_rerank_scores() -> None:  # 边界
    """候选太少跳过精排（没有分数）时，只靠条数与方向性判断。"""
    decision = gate_mod.gate_hits("违约金怎么算", [_hit(None), _hit(None)], qa_scorer=_high)  # 判决
    assert decision.top1 is None  # 没有分数
    assert decision.decision in {gate_mod.PASS, gate_mod.WEAK}  # 不因缺分误拒
    assert decision.kept  # 都留下


def test_gate_qa_unavailable_does_not_reject() -> None:  # 降级
    """reranker 不可用时只记原因，不能因此拒绝。"""
    decision = gate_mod.gate_hits("违约金怎么算", [_hit(3.0), _hit(2.0)], qa_scorer=lambda q, s: None)  # 判决
    assert decision.qa is None  # 无分
    assert "qa_unavailable" in decision.reasons  # 记原因
    assert decision.decision == gate_mod.PASS  # 仍通过
