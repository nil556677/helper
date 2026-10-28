"""Phase 5：trace 服务 + 会话持久化。"""

from __future__ import annotations

import asyncio

import pytest

from app.core.db import schema_v1
from app.core.db.connection import connect
from app.core.trace import store as trace_store
from app.services import conversations
from app.services import trace as trace_service


@pytest.fixture(autouse=True)
def _db(fake_root):
    conn = connect()
    try:
        schema_v1.initialize(conn)
    finally:
        conn.close()


def test_trace_service_page_stats_detail(fake_root):
    trace_store.create_run("r1", question="合同违约金", conversation_id="c1")
    trace_store.add_hits(
        "r1",
        [
            {"stage": "dense", "rank": 1, "chunk_key": "1:1:1", "text": "x"},
            {"stage": "fused", "rank": 1, "chunk_key": "1:1:1", "text": "x"},
        ],
    )
    trace_store.finish_run("r1", status="ok", total_tokens=12, ttft_ms=345)

    rows, total = trace_service.page(days=1)
    assert total == 1
    assert rows[0].hit_count == 2
    assert rows[0].status == "ok"
    assert rows[0].tokens == 12  # 列表要显示 tokens
    assert rows[0].ttft_ms == 345  # 列表要显示首字耗时

    stats = trace_service.stats(days=1)
    assert stats.total == 1 and stats.ok == 1 and stats.tokens == 12

    detail = trace_service.detail("r1")
    assert detail is not None and len(detail.hits) == 2

    exported = trace_service.export_json("r1")
    assert exported is not None and exported["run"]["id"] == "r1"


def test_trace_error_top(fake_root):
    """总览的"失败原因 Top"按次数倒序，且没失败时返回空。"""
    assert trace_service.error_top(days=1) == []  # 空库
    for index, (kind, status) in enumerate(
        [("timeout", "error"), ("timeout", "error"), ("model_auth", "error"), ("", "ok")]
    ):
        run_id = f"e{index}"
        trace_store.create_run(run_id, question="q")
        trace_store.finish_run(run_id, status=status, error_kind=kind)
    top = trace_service.error_top(days=1, limit=5)
    assert top[0] == ("timeout", 2)  # 最多的排第一
    assert ("model_auth", 1) in top  # 第二多
    assert all(kind for kind, _count in top)  # 空类型不出现


def test_conversation_store(fake_root):
    conversations.append_message("c1", role="user", content="你好", title="你好")
    conversations.append_message(
        "c1", role="assistant", content="你好呀", turn_json={"answer": "你好呀"}
    )
    items = conversations.list_conversations()
    assert items and items[0]["id"] == "c1"
    assert items[0]["title"] == "你好"
    messages = conversations.load_messages("c1")
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["turn_json"]


def test_rename_conversation(fake_root):
    """改名：trim / 空回退默认 / 超长截断 / 自定义名不被下一条消息覆盖。"""
    conversations.ensure_conversation("rc1", title="旧标题")
    # 正常改名（含首尾空白清理）
    assert conversations.rename_conversation("rc1", "  新标题  ") == "新标题"
    row = next(r for r in conversations.list_conversations() if r["id"] == "rc1")
    assert row["title"] == "新标题"
    # 空/全空白 → 回退默认标题
    assert conversations.rename_conversation("rc1", "   ") == "新对话"
    # 超长截断
    long_title = "甲" * 100
    assert conversations.rename_conversation("rc1", long_title) == "甲" * 40
    # 自定义名给下一条消息别被自动顶掉（append_message 只在仍为默认时才自动改名）
    conversations.rename_conversation("rc1", "我的名字")
    conversations.append_message("rc1", role="user", content="晚饭吃什么", title="晚饭吃什么")
    row = next(r for r in conversations.list_conversations() if r["id"] == "rc1")
    assert row["title"] == "我的名字"


def test_finalize_persists_messages(fake_root):
    from app.agent.nodes.finalize.node import finalize_node

    asyncio.run(
        finalize_node(
            {
                "conversation_id": "c2",
                "question": "合同违约金怎么算",
                "answer": "按每日万分之五。",
                "fragments": [{"index": 1}],
                "steps": [{"kind": "plan", "title": "理解问题"}],
            },
            {},
        )
    )
    messages = conversations.load_messages("c2")
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "合同违约金怎么算"
    assert messages[1]["content"] == "按每日万分之五。"
