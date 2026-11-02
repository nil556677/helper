"""运行传输单测：UI 只经 HttpClient → run_server（HTTP+SSE）访问 worker。

生产与测试走同一条路径：不再有「本进程直调」的旁路。这里用 httpx 的 ASGI 传输把
``run_server`` 拉成一个进程内 HTTP+SSE 服务，``HttpClient`` 照常按真实 HTTP 语义
打它——结构就是生产结构（UI → HttpClient → run_server → LocalBackend → 图 → SSE）。
"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 慢图占用循环
from typing import Any  # 任意值

import httpx  # ASGI 传输
import pytest  # 测试框架

from app.agent import stream as stream_mod  # 流式入口（图接缝）
from app.agent.contracts import DecisionTarget  # 决策目标（route.next）
from app.agent.events import EventType  # 事件类型
from app.agent.run import (  # 运行网关
    HttpClient,  # 远端实现
    LocalBackend,  # worker 内部后端
    RunInput,  # 入参
    RunSession,  # 会话
    get_run_client,  # 客户端工厂
    reset_client,  # 清缓存
)
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect  # 连接
from app.http.app import make_server  # HTTP+SSE 服务端


@pytest.fixture(autouse=True)
def _db(fake_root):  # 隔离库（drive 落 trace 需要）
    """每个用例一个空库。"""
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关


class _HappyGraph:  # 正常流假图
    """astream 按节点完成顺序 yield，让 driver 把状态机推到 done。"""

    async def astream(self, state: dict, config: dict, *, stream_mode=None):  # 流式入口
        """按父图节点顺序发 updates，最后一条 values 作权威快照。"""
        del state, config, stream_mode  # 不使用
        yield ("updates", {"load_context": {}})  # 上下文就绪
        yield ("updates", {"rag": {"route": {"next": DecisionTarget.ANSWER.value}}})  # 有证据
        yield ("updates", {"answer": {"answer": "ok"}})  # 生成完成
        yield ("updates", {"finalize": {}})  # 收尾完成 → done
        yield ("values", {"answer": "ok", "question": "问题", "conversation_id": "c1"})  # 快照


class _SlowGraph:  # 慢图
    """阻塞在 sleep 上，用来验证经 HTTP 的 cancel。"""

    async def astream(self, state: dict, config: dict, *, stream_mode=None):  # 流式入口
        """睡很久；取消应把它打断。"""
        del state, config, stream_mode  # 不使用
        await asyncio.sleep(10)  # 长阻塞
        yield ("values", {})  # 正常（到不了）


def _http_client(backend: LocalBackend) -> HttpClient:  # 造远端客户端
    """把 run_server 跑在 ASGI 传输上，返回打它的 HttpClient（结构同生产）。"""
    app = make_server(backend)  # HTTP+SSE 服务端（backend 即 worker 内部后端）
    transport = httpx.ASGITransport(app=app)  # 进程内 ASGI 传输
    inner = httpx.AsyncClient(transport=transport, base_url="http://test")  # 底层客户端
    return HttpClient("http://test", client=inner)  # 打 run_server


async def _drain(session: RunSession) -> list[Any]:  # 收完事件
    """消费到 sentinel，返回事件列表。"""
    out: list[Any] = []  # 收集
    while True:  # 直到结束
        ev = await session.events.get()  # 取
        if ev is RunSession.SENTINEL:  # 结束
            break  # 停
        out.append(ev)  # 记
    return out  # 返回


async def test_ui_talks_to_worker_over_http(monkeypatch) -> None:  # 主链路
    """UI 经 HttpClient→run_server→LocalBackend 拿到 ack 开头、done 结尾的完整事件流。"""
    monkeypatch.setattr(stream_mod, "_graph", lambda: _HappyGraph())  # worker 的图换假图
    session = await RunSession.create(  # 启动（内部起 _feed 消费 SSE）
        _http_client(LocalBackend()),  # HttpClient 打 run_server，后端是 worker
        RunInput(question="问题", conversation_id="c1"),  # 入参
    )  # 会话
    events = await _drain(session)  # 收完
    assert events[0].type is EventType.ACK  # 先回执
    assert events[-1].type is EventType.DONE  # 终态在最后
    assert not any(ev.type is EventType.ERROR for ev in events)  # 无错误


async def test_cancel_forwards_over_http(monkeypatch) -> None:  # 取消转发
    """经 HTTP 发 cancel，worker 收敛 CANCELLED 且不混 ERROR。"""
    monkeypatch.setattr(stream_mod, "_graph", lambda: _SlowGraph())  # worker 的图换慢图
    http = _http_client(LocalBackend())  # 构造客户端
    session = await RunSession.create(  # 启动
        http, RunInput(question="问题", conversation_id="c2")  # 入参
    )  # 会话
    session.cancel()  # 同步发停止（POST /cancel → backend.cancel）
    events = await _drain(session)  # 收完
    assert any(ev.type is EventType.CANCELLED for ev in events)  # 有取消终态
    assert not any(ev.type is EventType.ERROR for ev in events)  # 不混错误


def test_get_run_client_requires_worker(monkeypatch) -> None:  # 反：无 worker
    """没有 data/worker.port → 明确报错，不再回退本进程。"""
    reset_client()  # 清缓存
    monkeypatch.setattr("app.agent.run.factory._read_worker_port", lambda: 0)  # 模拟无端口
    try:  # 期望抛
        get_run_client()  # 调用
    except RuntimeError as exc:  # 捕获
        assert "worker 未就绪" in str(exc)  # 话术到位
    else:  # 没抛
        pytest.fail("worker 未就绪时应抛错，而不是回退")  # 失败
    reset_client()  # 清缓存


def test_get_run_client_returns_http_client(monkeypatch) -> None:  # 正：返回 HttpClient
    """有端口 → 返回 HttpClient（唯一的运行客户端）。"""
    reset_client()  # 清缓存
    monkeypatch.setattr("app.agent.run.factory._read_worker_port", lambda: 12345)  # 模拟端口
    try:  # 取
        client = get_run_client()  # 客户端
        assert isinstance(client, HttpClient)  # 是 HttpClient
    finally:  # 收尾
        reset_client()  # 清缓存
