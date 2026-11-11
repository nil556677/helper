"""关键词多字段：P1 字段抽取单测。"""

from __future__ import annotations

from app.core.sparse.fields import FIELD_CONTENT, FIELD_ENTITY, FIELD_KWD, FIELD_TITLE  # 字段名
from app.rag.kw_fields import chunk_field_texts, extract_important_kwd  # 抽取


def test_extract_important_kwd_low_df_only() -> None:  # kwd 只进命中且非高频
    terms = ["极速退", "订单", "云仓配"]  # 领域词（含高频"订单"）
    got = extract_important_kwd(  # 抽取
        "支持极速退，云仓配发货",  # 正文
        terms,  # 领域词
        high_df_words=frozenset({"订单"}),  # 订单高频，不该进
    )  # 结束
    assert got == ["极速退", "云仓配"]  # 命中且非高频


def test_chunk_field_texts_composes() -> None:  # chunk → 各字段文本
    fields = chunk_field_texts(  # 抽取
        content="订单 HT-2026-0043 支持极速退",  # 正文（含编号）
        title="售后政策",  # 标题
        tags=["售后"],  # 标签
        domain_terms=["极速退"],  # 领域词
    )  # 结束
    assert fields[FIELD_TITLE] == "售后政策"  # 标题字段
    assert fields[FIELD_CONTENT] == "订单 HT-2026-0043 支持极速退"  # 正文字段
    assert "售后" in fields[FIELD_ENTITY]  # 标签进实体
    assert "HT-2026-0043" in fields[FIELD_ENTITY]  # 编号被识别进实体（精确匹配杀手锏）
    assert fields[FIELD_KWD] == "极速退"  # 领域词进 kwd