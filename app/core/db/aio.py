"""异步 SQLite（aiosqlite）：async 连接的规范实现。

提供 `AsyncDB`：单连接 + 写锁（WAL/外键/busy_timeout），事务显式 BEGIN/COMMIT。

> **不要做跨事件循环复用的共享连接**：连接绑定 loop，跨 pytest 事件循环复用会卡死。
> 正确用法是"谁用谁开"：HTTP 路由按 ``app.state`` 缓存一份（见 ``app.http.deps``）；
> 同步线程/脚本仍走 ``app.core.db.connection``。
"""
from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 写锁
from pathlib import Path  # 路径
from typing import Any  # 任意值

import aiosqlite  # 异步 SQLite

#: busy_timeout：写锁等待上限（短，桌面场景写很短）。
_BUSY_TIMEOUT_MS = 5000


class AsyncDB:  # 异步数据库（单连接）
    """单个 aiosqlite 连接 + 写锁；事务交给调用方显式 BEGIN/COMMIT。"""

    def __init__(self, path: str | Path) -> None:  # 构造
        self._path = str(path)  # 库文件路径
        self._conn: aiosqlite.Connection | None = None  # 连接
        self._write_lock = asyncio.Lock()  # 写锁

    @property
    def path(self) -> str:  # 库文件路径
        return self._path  # 返回

    async def connect(self) -> None:  # 连接
        """建连接并设置 PRAGMA（WAL/外键/busy_timeout）。"""
        conn = await aiosqlite.connect(self._path)  # 连接
        conn.row_factory = aiosqlite.Row  # 行工厂
        await conn.execute("PRAGMA journal_mode=WAL")  # WAL 读写并发
        await conn.execute("PRAGMA synchronous=NORMAL")  # WAL 下性能/安全平衡
        await conn.execute("PRAGMA foreign_keys=ON")  # 外键级联
        await conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")  # 抢锁重试
        await conn.execute("PRAGMA temp_store=MEMORY")  # 临时表内存
        await conn.commit()  # 提交 PRAGMA
        self._conn = conn  # 存

    async def close(self) -> None:  # 关闭
        if self._conn is not None:  # 有连接
            await self._conn.close()  # 关
            self._conn = None  # 清

    @property
    def conn(self) -> aiosqlite.Connection:  # 连接
        assert self._conn is not None, "AsyncDB 未连接"  # 前置
        return self._conn  # 返回

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:  # 执行
        return await self.conn.execute(sql, params)  # 执行（调用方关 cursor/提交）

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:  # 查多行
        cur = await self.conn.execute(sql, params)  # 执行
        rows = await cur.fetchall()  # 取
        await cur.close()  # 关 cursor
        return [dict(row) for row in rows]  # 转 dict

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:  # 查一行
        cur = await self.conn.execute(sql, params)  # 执行
        row = await cur.fetchone()  # 取
        await cur.close()  # 关 cursor
        return dict(row) if row is not None else None  # 返回或 None

    @property
    def write_lock(self) -> asyncio.Lock:  # 写锁
        return self._write_lock  # 返回
