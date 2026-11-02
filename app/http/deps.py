"""依赖注入：backend / AsyncDB / 仓储。

路由统一经 ``Depends`` 注入；测试可用 ``dependency_overrides`` 替换。
AsyncDB 按 **每个 FastAPI app 缓存**（挂 app.state）：每个 app 一条连接、生命周期随 app，
规避跨事件循环复用连接导致测试卡住的问题。
"""
from __future__ import annotations  # 类型注解延迟求值

from fastapi import Depends, Request  # 依赖/请求

from app.core import paths  # 数据目录
from app.http.db import AsyncDB  # 异步连接
from app.http.feeds import ServerBackend  # 门面
from app.http.repositories.messages import MessageRepository  # 消息仓储
from app.http.repositories.runs import RunRepository  # 运行状态仓储


def get_backend(request: Request) -> ServerBackend:  # 取后端门面
    """从 app.state 取 ServerBackend（由 create_app 注入）。"""
    backend = getattr(request.app.state, "backend", None)  # 取
    assert backend is not None, "app.state.backend 未初始化"  # 前置
    return backend  # 返回


async def get_db(request: Request) -> AsyncDB:  # 取异步连接（每 app 缓存）
    """取 AsyncDB；未建则按 ``paths.db_file()`` 建一条并缓存到 app.state（每 app 一份）。"""
    db = getattr(request.app.state, "db", None)  # 取缓存
    if db is None:  # 没有
        db = AsyncDB(paths.db_file())  # 建（打开主库文件）
        await db.connect()  # 连接
        request.app.state.db = db  # 缓存
    return db  # 返回


def get_messages_repo(db: AsyncDB = Depends(get_db)) -> MessageRepository:  # 消息仓储
    return MessageRepository(db)  # 构造


def get_runs_repo(db: AsyncDB = Depends(get_db)) -> RunRepository:  # 运行状态仓储
    return RunRepository(db)  # 构造
