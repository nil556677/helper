"""关键词多字段 BM25：P0 字段常量 + 每字段参数与长度归一行为。"""

from __future__ import annotations

from app.core.sparse import bm25  # BM25 原语
from app.core.sparse import fields  # 字段常量


def test_field_weights_bounded() -> None:  # 权重比例有界（≤10:1）
    assert fields.weight_ratio_ok(10.0)  # 标题 5x / 正文 1x 等都在 10 以内
    assert fields.field_weight(fields.FIELD_TITLE) >= fields.field_weight(fields.FIELD_CONTENT)  # 标题 ≥ 正文
    assert fields.field_weight("未知字段") == 1.0  # 未知字段按基准，不炸
    max_ratio = max(fields.FIELD_WEIGHTS[k] for k in fields.FIELDS) / min(
        fields.FIELD_WEIGHTS[k] for k in fields.FIELDS if k != fields.FIELD_CODE
    )
    assert max_ratio <= 10.0  # 明确断言不超 10:1


def test_per_field_b_varied() -> None:  # 每字段 b 有区分(短字段更低)
    params = fields.per_field_params()  # 每字段参数
    assert params[fields.FIELD_TITLE]["b"] < params[fields.FIELD_CONTENT]["b"]  # 标题 b 低于正文
    assert params[fields.FIELD_ENTITY]["b"] < 0.5  # 实体极短 b 最低
    assert params[fields.FIELD_CONTENT]["b"] == 0.75  # 正文标准
    assert all(v["k1"] == 1.2 for v in params.values())  # k1 统一


def test_combine_field_scores_weighted() -> None:  # 多字段加权合并
    assert fields.combine_field_scores(  # 标题命中 → 5x
        {fields.FIELD_TITLE: 1.0}
    ) == 5.0
    assert fields.combine_field_scores(  # 正文命中 → 1x
        {fields.FIELD_CONTENT: 1.0}
    ) == 1.0
    assert fields.combine_field_scores(  # 标题+正文 → 加权和
        {fields.FIELD_TITLE: 1.0, fields.FIELD_CONTENT: 2.0}
    ) == 5.0 * 1.0 + 1.0 * 2.0
    assert fields.combine_field_scores(  # 未知字段按 1x
        {"未知字段": 3.0}
    ) == 3.0


def test_bm25_short_field_penalty_less() -> None:  # 短字段 b 低 → 同长度差惩罚更小
    # 构造长文档：dl 倍于 avgdl，短字段(b 低)应比长字段(b 高)惩罚少 → tf_norm 更高
    kw_b = fields.FIELD_B[fields.FIELD_KWD]  # 关键词字段 b
    ct_b = fields.FIELD_B[fields.FIELD_CONTENT]  # 正文 b
    tf, dl, avgdl = 1, 200, 100  # dl=2x avgdl
    short = bm25.tf_norm(tf, dl, avgdl, k1=1.2, b=kw_b)  # 低 b
    long_field = bm25.tf_norm(tf, dl, avgdl, k1=1.2, b=ct_b)  # 高 b
    assert short > long_field  # 低 b 的长度惩罚确实更轻