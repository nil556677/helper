"""异步会话仓储（ConversationAsyncStore）测试。"""
from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

import pytest  # 框架

from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect  # 连接


@pytest.fixture
def _db(fake_root):  # 隔离库
    """每个用例一个空库（建 v1 schema）。"""
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关


async def _store() -> Any:  # 异步连接 + 仓储
    from app.core import paths  # 数据目录
    from app.core.db.aio import AsyncDB  # 异步连接
    from app.core.db.asyncstore import ConversationAsyncStore  # 仓储

    db = AsyncDB(paths.db_file())  # 建连接
    await db.connect()  # 连接
    return db, ConversationAsyncStore(db)  # 返回


async def test_load_messages_async(_db) -> None:  # 读历史
    """append 一轮后，AsyncStore.load_messages 返回 user+assistant（与 sync 同形）。"""
    from app.services.conversations import append_turn  # 落库

    append_turn("c1", question="问题", answer="回答")  # 写一轮
    db, store = await _store()  # 建
    try:  # 用后关
        rows = await store.load_messages("c1")  # 读历史
        assert [r["role"] for r in rows] == ["user", "assistant"]  # 角色
        assert rows[1]["content"] == "回答"  # 回答
    finally:  # 收尾
        await db.close()  # 关连接


async def test_load_summary_async_default_and_saved(_db) -> None:  # 摘要
    """默认空摘要；save_summary 后可读回。"""
    from app.services.conversations import save_summary  # 落库

    db, store = await _store()  # 建
    try:  # 用后关
        empty = await store.load_summary("c1")  # 无摘要
        assert empty["summary"] == "" and empty["until_message_id"] == 0  # 空
        save_summary("c1", until_message_id=5, summary="摘要")  # 写
        got = await store.load_summary("c1")  # 读回
        assert got["summary"] == "摘要" and got["until_message_id"] == 5  # 有
    finally:  # 收尾
        await db.close()  # 关连接


async def test_trace_write_async(_db) -> None:  # trace 异步写
    """create/finish/update_run_async 走 AsyncDB，写进 trace_runs（单一异步写者）。"""
    from app.core import paths  # 数据目录
    from app.core.db.aio import AsyncDB  # 异步连接
    from app.core.db.connection import get_connection  # 同步读回
    from app.core.trace import store as trace_store  # trace 存储

    db = AsyncDB(paths.db_file())  # 建
    await db.connect()  # 连接
    try:  # 用后关
        await trace_store.create_run_async(db, "r-1", question="问题", conversation_id="c1")  # 建
        await trace_store.update_run_async(db, "r-1", answer="答")  # 更新
        await trace_store.finish_run_async(db, "r-1", status="ok", total_tokens=10)  # 收尾
    finally:  # 收尾
        await db.close()  # 关连接
    row = get_connection().execute(  # 同步读回
        "SELECT status, question, answer FROM trace_runs WHERE id = 'r-1'"  # 条件
    ).fetchone()  # 行
    assert row is not None  # 存在
    assert row["status"] == "ok"  # 状态
    assert row["question"] == "问题"  # 问题
    assert row["answer"] == "答"  # 答案


async def test_async_append_turn_and_dedup(_db) -> None:  # 异步写轮次 + 去重
    """append_turn 整轮写；预写 user 后 append_turn_existing_user 只补 assistant。"""
    from app.core import paths  # 数据目录
    from app.core.db.aio import AsyncDB  # 异步连接
    from app.core.db.asyncstore import ConversationAsyncStore  # 异步仓储
    from app.core.db.connection import get_connection  # 同步读回

    db = AsyncDB(paths.db_file())  # 建
    await db.connect()  # 连接
    try:  # 用后关
        store = ConversationAsyncStore(db)  # 仓储
        _u0, _a0 = await store.append_turn("c1", question="问题", answer="回答")  # 整轮
        await store.pending_user_by_run("c1", "run-x")  # 无预写 → None
        # 预写 user：经 append_turn 已有 user+assistant；再演示 existing_user 路径独立会话
        _u1, a1 = await store.append_turn_existing_user(  # 只补 assistant
            "c2", turn_id="7", answer="答2", title="新对话", turn_json={"answer": "答2"},
        )  # 补
        assert a1 > 0  # assistant id
    finally:  # 收尾
        await db.close()  # 关连接
    rows = get_connection().execute(  # 读 c2
        "SELECT role, status FROM messages WHERE conversation_id='c2' ORDER BY id"  # 条件
    ).fetchall()  # 行
    assert [r["role"] for r in rows] == ["assistant"]  # 只有 assistant（user 是 send期预写，未在此插）
    # c1 是整轮：user + assistant
    c1 = get_connection().execute(  # 读 c1
        "SELECT role FROM messages WHERE conversation_id='c1' ORDER BY id"  # 条件
    ).fetchall()  # 行
    assert [r["role"] for r in c1] == ["user", "assistant"]  # 整轮


async def test_trace_run_async_cm(_db) -> None:  # 异步 trace 会话
    """trace_run_async 用 AsyncDB 建/收 trace_runs，正常结束标 ok。"""
    from app.core import paths  # 数据目录
    from app.core.db.aio import AsyncDB  # 异步连接
    from app.core.db.connection import get_connection  # 同步读回
    from app.core.trace.recorder import trace_run_async  # 异步会话

    db = AsyncDB(paths.db_file())  # 建
    await db.connect()  # 连接
    try:  # 用后关
        async with trace_run_async(db, question="问题", conversation_id="c1") as session:  # 会话
            assert session.run_id  # 有 id
        run_id = session.run_id  # 记
    finally:  # 收尾
        await db.close()  # 关连接
    row = get_connection().execute(  # 同步读回
        "SELECT status, question FROM trace_runs WHERE id = ?", (run_id,)  # 条件
    ).fetchone()  # 行
    assert row is not None  # 存在
    assert row["status"] == "ok"  # 正常结束
    assert row["question"] == "问题"  # 问题
