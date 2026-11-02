"""HTTP API 契约测试：统一 envelope、消息/运行状态端点、404 错误体。"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Iterator  # 迭代器

import httpx  # ASGI
import pytest  # 框架

from app.agent.run import LocalBackend  # 后端
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


@pytest.fixture
async def _client(_db, tmp_path) -> Iterator[httpx.AsyncClient]:  # 建 ASGI 客户端
    """把 make_server(LocalBackend()) 跑在 ASGI 上，返回可打的客户端。"""
    from app.http.app import make_server  # 装配

    app = make_server(LocalBackend())  # app（含后端 + 路由）
    transport = httpx.ASGITransport(app=app)  # 进程内 ASGI
    client = httpx.AsyncClient(transport=transport, base_url="http://test")  # 客户端
    try:  # 用后关
        yield client  # 交给用例
    finally:  # 收尾
        await client.aclose()  # 关 http 客户端
        db = getattr(app.state, "db", None)  # 取缓存连接
        if db is not None:  # 有
            await db.close()  # 关 aiosqlite


def _insert_trace(run_id: str, status: str = "ok") -> None:  # 插 trace_runs
    from app.core.db.connection import get_connection  # 连接

    get_connection().execute(  # 插入
        "INSERT INTO trace_runs(id, conversation_id, status, question, started_at) VALUES (?,?,?,?,?)",
        (run_id, "c1", status, "问题", 1700000000.0),
    )  # 结束
    get_connection().commit()  # 提交


async def test_get_messages_returns_history(_client) -> None:  # 历史
    """append 一轮后，GET messages 返回 user+assistant（chat 轨迹）。"""
    from app.services.conversations import append_turn  # 落库

    append_turn("c1", question="问题", answer="回答")  # 写一轮
    resp = await _client.get("/api/v1/conversations/c1/messages")  # 查
    body = resp.json()  # 解析
    assert body["code"] == 0  # 成功
    roles = [item["role"] for item in body["data"]["items"]]  # 角色
    assert roles == ["user", "assistant"]  # 顺序
    assert body["data"]["items"][1]["content"] == "回答"  # 回答内容


async def test_get_message_missing_returns_404(_client) -> None:  # 404
    """不存在的消息 → 404 + 统一错误体。"""
    resp = await _client.get("/api/v1/conversations/c1/messages/999")  # 查
    assert resp.status_code == 404  # 状态码
    body = resp.json()  # 解析
    assert body["code"] == 40401  # 业务码
    assert "data" in body  # 有 data 键


async def test_get_run_returns_status(_client) -> None:  # 运行状态
    """有 trace_runs → GET /runs/{id} 返回状态。"""
    _insert_trace("r-1")  # 插记录
    resp = await _client.get("/api/v1/runs/r-1")  # 查
    assert resp.json()["data"]["status"] == "ok"  # 状态
    resp_miss = await _client.get("/api/v1/runs/nope")  # 查缺
    assert resp_miss.status_code == 404  # 404
    assert resp_miss.json()["code"] == 40402  # 业务码


async def test_create_run_bad_input_422(_client) -> None:  # 校验 422
    """空 question → 422 + 统一错误体。"""
    resp = await _client.post("/api/v1/runs", json={"question": "", "conversation_id": "c"})  # 发空
    assert resp.status_code == 422  # 校验失败
    assert resp.json()["code"] == 40001  # 业务码（参数错误）
