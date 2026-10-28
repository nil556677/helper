"""Run RPC 服务端（E0.2）：把任意 ``RunClient`` 后端暴露成 HTTP + SSE。

- ``POST /runs``：启动一轮，返回 run_id；
- ``GET /runs/{run_id}/events``：SSE 事件流（支持 ``Last-Event-ID`` 续传）；
- ``POST /runs/{run_id}/cancel``、``POST /runs/{run_id}/approve``：控制。

**Feed 设计**：每个 run 起一个后台消费任务，把 ``backend.events(run_id)`` 按
单调序号灌进内存缓冲（``(seq, event)``）；SSE 订阅者从缓冲读、按 ``last_event_id``
续传。这样「多个订阅者 + 断线重连」才有稳定序号；命令（start/cancel/approve）原样转发。

分层：本地 worker = ``InProcessClient``（真业务）作为这个后端；UI 用 ``HttpClient`` 远端访问。
本模块不 import ``app.agent.*`` 的图/驱动，只依赖 ``RunClient`` 协议。
"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 队列/事件
from collections import deque  # 有界缓冲
from collections.abc import AsyncIterator  # 类型
from typing import Any

from fastapi import FastAPI, HTTPException, Request  # Web 框架
from fastapi.responses import StreamingResponse  # SSE 响应

from app.agent.run_client import RunClient, RunInput  # 协议与入参

#: 每 run 事件缓冲上限（够断线续传；history 回看走 DB，不靠它）。
_BUFFER_CAP = 4000
#: SSE 心跳间隔（秒）：防代理/空闲断开。
_HEARTBEAT_S = 15.0
#: 结束后的 feed 保留时长（秒）：留出断线重连窗口，之后回收（防内存随轮次增长）。
_FEED_TTL_S = 600.0


class _Feed:  # 单 run 的事件缓冲 + 后台消费
    """一个 run 一次只消费它的 events() 一次，全部进缓冲；SSE 从缓冲读。"""

    def __init__(self, backend: RunClient, run_id: str) -> None:  # 构造
        self.backend = backend  # 后端
        self.run_id = run_id  # run id
        self.seq = 0  # 单调序号
        self.buf: deque[tuple[int, Any]] = deque(maxlen=_BUFFER_CAP)  # (seq, event)
        self.done = False  # 是否消费完
        self.wake: asyncio.Event = asyncio.Event()  # 有新数据/结束唤醒
        self._task: asyncio.Task | None = None  # 后台任务

    async def run(self) -> None:  # 消费 loops
        """消费后端事件流；结束置 done 并唤醒订阅者。"""
        try:  # 逐条
            async for event in self.backend.events(self.run_id):  # 后端流
                self.seq += 1  # 递增
                self.buf.append((self.seq, event))  # 入缓冲
                self.wake.set()  # 唤醒
        finally:  # 结束
            self.done = True  # 标记完成
            self.wake.set()  # 再唤醒

    def emit_pending(self, after: int) -> list[tuple[int, Any]]:  # 取某序号之后
        """返回 ``(seq, event)`` 中 > after 的部分（已排序）。"""
        return [(seq, e) for seq, e in self.buf if seq > after]  # 过滤


async def _wait_for_more(feed: _Feed, seen: int) -> bool:  # 等下一条可发的事件
    """等新数据；``True``=有新数据可发，``False``=空闲（调用方发心跳）。

    **必须先清 ``wake`` 再等**：它是「有新数据」的电平信号，不清就一直为真，
    而 ``asyncio.Event.wait()`` 在已 set 时立即返回、**不挂起**——调用方会变成
    不挂起的死循环，把整个事件循环占死，SSE 一个字节都发不出去（真实踩过：
    客户端只拿到 200 头，body 永远等不到，最终被读超时误杀）。
    """
    feed.wake.clear()  # 清掉旧唤醒（电平信号）
    if feed.buf and feed.buf[-1][0] > seen:  # 清完又出现了新数据（并发写入）
        return True  # 别等，马上发
    try:  # 等新数据
        await asyncio.wait_for(feed.wake.wait(), timeout=_HEARTBEAT_S)  # 唤醒或超时
    except TimeoutError:  # 空闲
        return False  # 让调用方发心跳
    return True  # 有新数据


class ServerBackend:  # 服务端门面
    """命令转发 + feed 管理。"""

    def __init__(self, backend: RunClient) -> None:  # 构造
        self.backend = backend  # 真正的后端
        self.feeds: dict[str, _Feed] = {}  # run_id → feed

    async def create(self, run_input: RunInput) -> str:  # 启一轮
        run_id = await self.backend.start(run_input)  # 启动
        feed = _Feed(self.backend, run_id)  # 建 feed
        self.feeds[run_id] = feed  # 记录
        loop = asyncio.get_running_loop()  # 当前 loop
        feed._task = loop.create_task(feed.run())  # 后台消费
        # **必须有回收**：feed 里带着整轮事件缓冲（最多 _BUFFER_CAP 条）。只进不出的话，
        # worker 长跑下去内存会随轮次一直涨（与 checkpoint 同属"资源没人释放"）。
        loop.call_later(_FEED_TTL_S, self._evict, run_id, feed)  # 定时回收
        return run_id  # run id

    def feed(self, run_id: str) -> _Feed | None:  # 取 feed
        return self.feeds.get(run_id)  # 查

    def _evict(self, run_id: str, feed: _Feed) -> None:  # 回收一个 feed
        """已结束且过了保留期就摘掉；还在跑就顺延（不打扰长轮次）。"""
        if self.feeds.get(run_id) is not feed:  # 已被替换/回收
            return  # 结束
        if not feed.done:  # 还在跑
            asyncio.get_running_loop().call_later(_FEED_TTL_S, self._evict, run_id, feed)  # 顺延
            return  # 结束
        self.feeds.pop(run_id, None)  # 摘掉（断线重连窗口已过）


def make_server(backend: RunClient, *, on_startup: Any | None = None) -> FastAPI:  # 建 ASGI app
    """以任意 RunClient 后端构造 HTTP 服务；on_startup 为可选的异步启动钩子（E2/E0.4）。"""
    from contextlib import asynccontextmanager  # lifespan

    core = ServerBackend(backend)  # 门面

    @asynccontextmanager  # lifespan
    async def lifespan(_app: FastAPI):  # 进出事件
        if on_startup is not None:  # 有钩子
            await on_startup()  # 于服务 loop 执行（建 checkpointed 图等）
        yield  # 放行

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)  # app（开 lifespan）

    @app.post("/runs")  # 起一轮
    async def create_run(request: Request) -> dict[str, str]:  # 返回 run_id
        body = await request.json()  # JSON
        run_id = await core.create(  # 启动
            RunInput(  # 入参
                question=str(body.get("question") or ""),  # 问题
                conversation_id=str(body.get("conversation_id") or "default"),  # 会话
                attachments=list(body.get("attachments") or []),  # 附件
            )  # 入参结束
        )  # 启动结束
        return {"run_id": run_id}  # 返回

    @app.get("/runs/{run_id}/events")  # 事件流
    async def stream_events(run_id: str, request: Request) -> StreamingResponse:  # SSE
        feed = core.feed(run_id)  # 找 feed
        if feed is None:  # 不存在
            raise HTTPException(status_code=404, detail="unknown run")  # 404
        last = _last_event_id(request)  # 续传点

        async def generate() -> AsyncIterator[str]:  # SSE 生成器
            seen = last  # 已发到的序号
            while True:  # 长连接
                for seq, event in feed.emit_pending(seen):  # 新事件
                    seen = seq  # 更新
                    yield f"id: {seq}\ndata: {event.model_dump_json()}\n\n"  # 报文
                if feed.done:  # 已消费完
                    break  # 关流
                if not await _wait_for_more(feed, seen):  # 等新数据（内部会先清 wake，见该函数说明）
                    yield ": ping\n\n"  # 空闲心跳

        return StreamingResponse(  # SSE 响应
            generate(),  # 生成器
            media_type="text/event-stream",  # 类型
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},  # 头
        )

    @app.post("/runs/{run_id}/cancel")  # 取消
    async def cancel_run(run_id: str) -> dict[str, bool]:  # ok
        await core.backend.cancel(run_id)  # 转发
        return {"ok": True}  # 返回

    @app.post("/runs/{run_id}/approve")  # 审批
    async def approve_run(run_id: str, request: Request) -> dict[str, bool]:  # ok
        body = await request.json()  # JSON
        await core.backend.approve(  # 转发
            run_id,  # run
            str(body.get("request_id") or ""),  # 请求 id
            bool(body.get("allow")),  # 允许
        )  # 转发结束
        return {"ok": True}  # 返回

    return app  # 返回


def _last_event_id(request: Request) -> int:  # 取续传点
    """优先 header，其次 query。"""
    raw = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id") or ""  # 取
    try:  # 转数字
        return int(raw)  # 返回
    except (TypeError, ValueError):  # 非数字
        return 0  # 从头


def serve_in_thread(  # 起服务
    backend: RunClient,  # 后端
    *,  # 仅关键字
    port_file: str = "",  # 端口文件
    startup: Any | None = None,  # 启动钩子（async，见 make_server）
) -> tuple[Any, int]:
    """在守护线程里起 uvicorn，监听 127.0.0.1 动态端口；把端口写给 port_file（可选）。

    返回：``(server, port)``，调用方设 ``server.should_exit = True`` 可停。
    供 worker 进程使用（本进程内 InProcessClient 即真业务）。
    """
    import socket  # 找空闲端口
    import threading  # 线程

    import uvicorn  # ASGI 服务

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:  # 占一个空闲口
        probe.bind(("127.0.0.1", 0))  # 绑定
        port = int(probe.getsockname()[1])  # 拿到端口
    config = uvicorn.Config(  # 配置
        make_server(backend, on_startup=startup),  # app（带启动钩子）
        host="127.0.0.1",  # 只监听本地
        port=port,  # 动态端口
        log_level="warning",  # 少日志
        lifespan="on",  # 开 lifespan，跑启动钩子
    )  # 配置结束
    server = uvicorn.Server(config)  # 服务
    thread = threading.Thread(target=server.run, daemon=True, name="run-rpc")  # 守护线程
    thread.start()  # 起
    if port_file:  # 要写端口文件
        from pathlib import Path  # 路径

        target = Path(port_file)  # 文件
        target.parent.mkdir(parents=True, exist_ok=True)  # 建目录
        target.write_text(str(port), encoding="utf-8")  # 写端口
    return server, port  # 返回
