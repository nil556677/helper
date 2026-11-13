"""trace 存储写路径回归：同步/异步两条路径行为等价（收尾逻辑不因收敛漂移）。

背景：``create_run / update_run / finish_run`` 原本同步异步各复制一份 SQL 与参数，
改一处漏另一处会产生 run 摘要字段漂移。已收敛为共用 ``_CREATE_RUN_SQL`` /
``_FINISH_RUN_SQL`` / ``_update_clause`` 等，这里锁住"两条路径产出等价行"。
"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 跑异步
from typing import Any  # 任意值

import pytest  # 框架

from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect, get_connection  # 同步连接


@pytest.fixture
def _db(fake_root):  # 隔离库
    """每个用例一个空库（建 v1 schema，含 trace_runs）。"""
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关


def _row(run_id: str) -> Any:  # 读一行
    return get_connection().execute("SELECT * FROM trace_runs WHERE id=?", (run_id,)).fetchone()  # 行


def test_sync_trace_write_consolidated(_db) -> None:  # 同步路径
    """同步 create/update/finish 后行状态正确（不含 token/时长计算漂移）。"""
    from app.core.trace import store as trace_store  # trace 存储

    trace_store.create_run("r1", question="问题", conversation_id="c1")  # 建
    trace_store.update_run("r1", answer="答", ttft_ms=5)  # 更新（白名单字段）
    trace_store.finish_run("r1", status="ok", prompt_tokens=10, completion_tokens=20)  # 收尾
    row = _row("r1")  # 读回
    assert row["status"] == "ok"  # 状态
    assert row["question"] == "问题"  # 问题
    assert row["answer"] == "答"  # 摘要
    assert row["ttft_ms"] == 5  # COALESCE 保留已设的首 token
    assert row["total_tokens"] == 30  # 10 + 20
    assert row["duration_ms"] >= 0  # 时长已算


def test_sync_async_paths_equivalent(_db) -> None:  # 双路径等价
    """同名 run 走同步与异步两条路径，关键摘要字段必须一致。"""
    from app.core import paths  # 数据目录
    from app.core.db.aio import AsyncDB  # 异步连接
    from app.core.trace import store as trace_store  # trace 存储

    # 同步路径
    trace_store.create_run("ra", question="问题", conversation_id="c")  # 建
    trace_store.update_run("ra", answer="答")  # 更新
    trace_store.finish_run("ra", status="ok", prompt_tokens=10, completion_tokens=20)  # 收尾
    # 异步路径（同一库文件）
    db = AsyncDB(paths.db_file())  # 异步连接
    try:  # 用后关
        async def _run() -> None:  # 异步写
            await db.connect()  # 连
            await trace_store.create_run_async(db, "rb", question="问题", conversation_id="c")  # 建
            await trace_store.update_run_async(db, "rb", answer="答")  # 更新
            await trace_store.finish_run_async(db, "rb", status="ok", prompt_tokens=10, completion_tokens=20)  # 收尾

        asyncio.run(_run())  # 跑异步
    finally:  # 收尾
        asyncio.run(db.close())  # 关

    a = _row("ra")  # 同步行
    b = _row("rb")  # 异步行
    for field in ("status", "question", "answer", "total_tokens", "ttft_ms"):  # 关键摘要字段
        assert a[field] == b[field], f"{field} 两条路径不一致"  # 等价
    # 时长是墙钟差、非确定值，不断言相等，只确认两条路径都算出来了
    assert int(a["duration_ms"]) >= 0 and int(b["duration_ms"]) >= 0  # 都已算时长
