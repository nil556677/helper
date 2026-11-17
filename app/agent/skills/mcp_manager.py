"""MCP 连接生命周期管理（P3）：连接复用、空闲回收、优雅关闭。

- 复用 `mcp._make_client`（可打桩）；一个 server 一条长连接，工具装载后缓存；
- `evict_idle(max_idle_s)` 回收空闲连接；`close_all()` 供应用退出时优雅关闭（无孤儿子进程）。
- 本模块只管连接泵，不碰审批/门控（由调用方 `gated` 决定是否装载）。
"""

from __future__ import annotations  # 延迟值注解

import time  # 时间
from typing import Any  # 任意值

__all__ = ["McpManager"]  # 导出


class McpManager:  # 连接泵
    """server_id → (client, tools, last_used)。线程内单实例；退出必须 close_all。"""

    def __init__(self) -> None:  # 构造
        self._clients: dict[str, Any] = {}  # 连接
        self._tools: dict[str, list[Any]] = {}  # 工具缓存
        self._last_used: dict[str, float] = {}  # 最近使用

    async def get_tools(self, server_id: str, spec: dict[str, Any], *, config: Any | None = None) -> list[Any]:  # 取工具（复用连接）
        if server_id in self._tools:  # 已缓存
            self._last_used[server_id] = time.monotonic()  # 更新时间
            return list(self._tools[server_id])  # 返回
        from app.agent.skills import mcp  # 消费模块

        client = mcp._make_client({server_id: spec})  # 建客户端（可打桩）
        await client.__aenter__()  # 进入（保留连接）
        try:  # 载工具
            remote = await client.get_tools()  # 远端工具
            tools = [mcp.adopt(t, config=config) for t in remote]  # 翻译
        except Exception:  # 装载失败
            await self._close_client(server_id)  # 关掉半开连接
            raise  # 上抛
        self._clients[server_id] = client  # 存连接
        self._tools[server_id] = tools  # 存工具
        self._last_used[server_id] = time.monotonic()  # 时间
        return list(tools)  # 返回

    async def evict_idle(self, max_idle_s: float, *, now: float | None = None) -> list[str]:  # 回收空闲连接
        current = time.monotonic() if now is None else float(now)  # 当前
        evicted: list[str] = []  # 结果
        for sid in list(self._clients):  # 遍历
            if current - self._last_used.get(sid, current) >= max_idle_s:  # 超时
                await self._close_client(sid)  # 关
                evicted.append(sid)  # 记
        return evicted  # 返回

    async def close_all(self) -> None:  # 优雅关闭全部连接
        for sid in list(self._clients):  # 逐个
            await self._close_client(sid)  # 关

    async def _close_client(self, server_id: str) -> None:  # 关单个（幂等）
        client = self._clients.pop(server_id, None)  # 取
        self._tools.pop(server_id, None)  # 清工具
        self._last_used.pop(server_id, None)  # 清时间
        if client is None:  # 本来就没
            return  # 无事
        try:  # 关闭
            await client.__aexit__(None, None, None)  # 退出上下文
        except Exception:  # 关失败不影响
            return  # 忽略

    def open_servers(self) -> list[str]:  # 当前打开的 server（观测/排查）
        return list(self._clients)  # 返回
