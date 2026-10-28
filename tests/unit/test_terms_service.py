"""领域词服务层（`app/services/terms.py`）：新增/列出/重复/停用/删除。

顺带验证 ``list_terms()`` 确实能返回"只读即见"的数据（界面"列表无数据"不该是数据层问题）。
"""

from __future__ import annotations

import pytest

from app.core.db import schema_v1
from app.core.db.connection import get_connection
from app.core.retrieval import terms as core
from app.services import terms


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = get_connection()  # 与 core.terms 同一条连接
    try:
        schema_v1.initialize(conn)  # 建 token_dict 等表
        conn.execute("DELETE FROM token_dict")  # 每个用例清空，避免词条跨用例残留相撞
        conn.commit()
    finally:
        pass


def test_terms_add_list(fake_root):
    """新增后能列出来（列表的数据来源）。"""
    terms.add_term("履约保证金", freq=100)  # 带词频
    terms.add_term("供应商")  # 默认词频
    rows = terms.list_terms()  # 全部
    by_term = {row["term"]: row for row in rows}  # 索引
    assert set(by_term) == {"履约保证金", "供应商"}  # 都在
    assert by_term["履约保证金"]["freq"] == 100  # 词频保留
    assert by_term["供应商"]["freq"] == 1  # 不带词频→默认 1（不误报"已存在"）
    assert by_term["供应商"]["enabled"] is True  # 默认启用


def test_terms_duplicate_raises(fake_root):
    """重复添加抛 TermError（界面据此提示"已存在"）。"""
    terms.add_term("履约保证金")
    with pytest.raises(core.TermError):  # 重复
        terms.add_term("履约保证金")


def test_terms_toggle_delete(fake_root):
    """停用 / 删除生效。"""
    terms.add_term("供应商")
    terms.set_enabled("供应商", False)  # 停用
    row = next(r for r in terms.list_terms() if r["term"] == "供应商")  # 找到
    assert row["enabled"] is False  # 已停用
    assert terms.delete_term("供应商") is True  # 删除成功
    assert not [r for r in terms.list_terms() if r["term"] == "供应商"]  # 没了
