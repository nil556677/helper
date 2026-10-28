"""M4 L2 支撑校验：字面、否定反转、数字冲突、语义兜底、以及"只标注不删句"。"""

from __future__ import annotations  # 类型注解延迟求值

from app.rag.verify import support, verify  # 被测

EVIDENCE = [  # 两条证据
    {"index": 1, "text": "违约金上限为三十万元，超过部分不予支持。"},  # 第 1 条
    {"index": 2, "text": "乙方应在每月 5 日前支付服务费。"},  # 第 2 条
]  # 列表结束


def test_supported_when_sentence_comes_from_evidence() -> None:  # L2-01 正
    """句子基本照抄证据 → 支撑。"""
    bad = support("违约金上限为三十万元[1]。", EVIDENCE)  # 校验
    assert bad == []  # 没有未支撑


def test_unsupported_when_citation_points_elsewhere() -> None:  # L2-02 反
    """引用 [1]，但句子其实来自 [2] → 未支撑。"""
    bad = support("乙方应在每月 5 日前支付服务费[1]。", EVIDENCE)  # 校验
    assert bad == [0]  # 第一句未支撑


def test_unsupported_on_negation_flip() -> None:  # L2-03 反
    """证据说"不予支持"，答案写"予以支持" → 极性反转。"""
    bad = support("违约金超过部分予以支持[1]。", EVIDENCE)  # 校验
    assert bad == [0]  # 未支撑


def test_unsupported_on_number_conflict() -> None:  # L2-04 反
    """证据 30 万，答案写 50 万 → 数字冲突。"""
    bad = support("违约金上限为五十万元[1]。", EVIDENCE)  # 校验
    assert bad == [0]  # 未支撑


def test_semantic_fallback_accepts_synonym() -> None:  # L2-05 正
    """字面不过但有语义层 → 支撑（押金/保证金）。"""
    evidence = [{"index": 1, "text": "保证金在合同期满后退还。"}]  # 证据
    def embedder(sentence: str, source: str) -> float:  # 假嵌入
        """只要都提到"退"就给高分。"""
        return 0.9 if "退" in sentence and "退" in source else 0.1  # 相似度
    bad = support("押金在合同到期后会退回[1]。", evidence, embedder=embedder)  # 校验
    assert bad == []  # 语义层兜住了


def test_semantic_layer_absent_marks_unsupported() -> None:  # L2-05 反（无嵌入时）
    """没有语义层时同义表达会被判未支撑——这是已知代价，只标注不删句。"""
    evidence = [{"index": 1, "text": "保证金在合同期满后退还。"}]  # 证据
    bad = support("押金在合同到期后会退回[1]。", evidence)  # 校验
    assert bad == [0]  # 字面不过


def test_sentence_without_citation_is_skipped() -> None:  # L2-06 正（段级引用）
    """没有角标的句子不参与判定，只判带角标的那句（不误伤）。"""
    bad = support("先说背景。违约金上限为三十万元[1]。", EVIDENCE)  # 校验
    assert bad == []  # 两句都没被判未支撑


def test_verify_includes_unsupported() -> None:  # 集成
    """总入口把未支撑句子带进结果与原因。"""
    result = verify("违约金超过部分予以支持[1]。", EVIDENCE)  # 校验（不查库）
    assert result.valid == [1]  # 编号有效
    assert result.unsupported == [0]  # 第 1 句未支撑
    assert "unsupported" in result.reasons  # 原因里有
    assert "[1]" in result.text  # 只标注，不删角标


def test_l2_can_be_disabled() -> None:  # 开关
    """`l2=False` 时不算支撑（给"只要编号对就行"的场景）。"""
    result = verify("违约金超过部分予以支持[1]。", EVIDENCE, l2=False)  # 校验
    assert result.unsupported == []  # 不算
