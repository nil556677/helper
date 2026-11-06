"""会话隔离：删除后不被进行中的 run 复活；写入严格按 conversation_id 隔离。"""

from __future__ import annotations

import pytest

from app.core.db import schema_v1
from app.core.db.connection import close_thread_connection, connect, get_connection
from app.services import conversations as service


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()
    yield
    close_thread_connection()


def test_isolation_between_conversations() -> None:  # 不同会话互不串
    service.append_turn("a", question="A问", answer="A答")
    service.append_turn("b", question="B问", answer="B答")
    rows_a = service.load_messages("a")
    rows_b = service.load_messages("b")
    assert [row["content"] for row in rows_a if row["role"] == "user"] == ["A问"]  # A 只有自己的
    assert [row["content"] for row in rows_b if row["role"] == "user"] == ["B问"]  # B 只有自己的


def test_delete_tombstone_prevents_resurrect() -> None:  # 删除后不许重建（进行中的 run 收尾）
    service.append_turn("c", question="问题", answer="回答")
    service.delete_conversation("c")
    assert service.tombstoned("c") is True  # 有墓碑
    assert service.list_conversations() == []  # 列表已无
    # 模拟进行中的 run 收尾：ensure/append 都不该让会话复活
    service.ensure_conversation("c", title="复活")
    result = service.append_turn("c", question="补", answer="补答")
    assert result == (-1, -1)  # 跳过写入
    assert service.list_conversations() == []  # 仍未复活
    assert service.load_messages("c") == []  # 没有消息


def test_delete_cascades_messages() -> None:  # 删除级联删消息行
    service.append_turn("d", question="问题", answer="回答")
    service.delete_conversation("d")
    left = get_connection().execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id = 'd'").fetchone()["n"]
    assert int(left) == 0  # 级联清空