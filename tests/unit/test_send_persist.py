"""发送即落库 + persist 去重测试。"""
from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

import pytest  # 框架

from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect, get_connection  # 连接


@pytest.fixture
def _db(fake_root):  # 隔离库
    """每个用例一个空库（建 v1 schema，含 run_id 列）。"""
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关


def _count() -> tuple[int, int]:  # 统计角色数
    rows = get_connection().execute(  # 查
        "SELECT role, status FROM messages ORDER BY id"  # 角色/状态
    ).fetchall()  # 行
    return (  # 返回
        sum(1 for r in rows if r["role"] == "user"),  # user 数
        sum(1 for r in rows if r["role"] == "assistant"),  # assistant 数
    )  # 结束


class _DB:  # 异步连接壳（直接用于仓储测试）
    def __init__(self) -> None:  # 构造
        from app.core import paths  # 数据目录
        from app.http.db import AsyncDB  # 异步连接

        self.db = AsyncDB(paths.db_file())  # 建连接对象

    async def __aenter__(self) -> Any:  # 进入
        await self.db.connect()  # 连接
        return self.db  # 返回

    async def __aexit__(self, *_a: Any) -> None:  # 退出
        await self.db.close()  # 关


async def test_insert_user_writes_running_message(_db) -> None:  # 发送即落库
    """insert_user 写入一条进行中的 user 消息（status=running，带 run_id）。"""
    from app.http.repositories.messages import MessageRepository  # 仓储

    async with _DB() as db:  # 连接
        repo = MessageRepository(db)  # 仓储
        message_id = await repo.insert_user("c1", "问题", "run-1")  # 插入
        data = await repo.list("c1")  # 查历史
        assert len(data["items"]) == 1  # 一条
        assert data["items"][0]["role"] == "user"  # user
        assert data["items"][0]["content"] == "问题"  # 内容
    row = get_connection().execute(  # 校验库内字段
        "SELECT run_id, status FROM messages WHERE id = ?", (message_id,)  # 条件
    ).fetchone()  # 行
    assert row["run_id"] == "run-1"  # run 链接
    assert row["status"] == "running"  # 进行中


async def test_delete_removes_ghost_message(_db) -> None:  # 创建失败补偿
    """delete 能删掉预写的 user 消息（创建失败时路由用它清"幽灵消息"）。"""
    from app.http.repositories.messages import MessageRepository  # 仓储

    async with _DB() as db:  # 连接
        repo = MessageRepository(db)  # 仓储
        message_id = await repo.insert_user("c1", "问题", "run-1")  # 预写 user
        await repo.delete("c1", message_id)  # 删掉
        data = await repo.list("c1")  # 查历史
        assert data["items"] == []  # 已清空
    row = get_connection().execute("SELECT COUNT(*) AS n FROM messages", ()).fetchone()  # 统计
    assert row["n"] == 0  # 库里没残留


async def test_persist_turn_dedups_user(_db) -> None:  # persist 去重
    """已预写 user → finalize 只补 assistant，不重复插 user，且把 user 标完成。"""
    from app.agent.context import RunContext  # 上下文
    from app.agent.persist import persist_turn  # 落库
    from app.http.repositories.messages import MessageRepository  # 仓储

    async with _DB() as db:  # 连接
        repo = MessageRepository(db)  # 仓储
        message_id = await repo.insert_user("c1", "问题", "run-1")  # 预写 user

    ctx = RunContext(run_id="run-1", question="问题")  # 运行上下文
    ok = persist_turn(ctx, conversation_id="c1", question="问题", answer="回答")  # 落库
    assert ok is True  # 成功
    assert ctx.persisted is True  # 已标记
    assert ctx.turn_id == str(message_id)  # 轮次 id = 预写 user id
    users, assistants = _count()  # 计数
    assert users == 1  # 只有一条 user
    assert assistants == 1  # 一条 assistant
    rows = get_connection().execute(  # 校验状态
        "SELECT status, content FROM messages WHERE role = 'user'"  # user 行
    ).fetchall()  # 行
    assert rows[0]["status"] == "ok"  # user 已完成
    asst = get_connection().execute(  # 校验 assistant 内容
        "SELECT content FROM messages WHERE role = 'assistant'"  # assistant 行
    ).fetchone()  # 行
    assert asst["content"] == "回答"  # 答案


async def test_persist_turn_legacy_writes_pair(_db) -> None:  # 未预写 → 整轮
    """没有预写 user → persist_turn 仍原子写 user+assistant（非 HTTP 路径兼容）。"""
    from app.agent.context import RunContext  # 上下文
    from app.agent.persist import persist_turn  # 落库

    ctx = RunContext(run_id="run-x", question="问题")  # 上下文（无预写）
    persist_turn(ctx, conversation_id="cx", question="问题", answer="答")  # 落库
    users, assistants = _count()  # 计数
    assert users == 1  # user
    assert assistants == 1  # assistant
    assert ctx.turn_id == "1"  # turn_id = user 消息 id（首条）
