"""RunGateway 协议：UI 与服务端共用的运行接口。

只暴露 start/events/cancel/approve 四个方法；UI 的 ``streaming.py`` 只依赖它，
不关心下面是 ``HttpClient``（打 worker）还是 ``LocalBackend``（worker 内部）还是测试假实现。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import AsyncIterator  # 异步迭代
from typing import Any, Protocol  # 协议

from app.agent.run.types import RunInput  # 入参


class RunGateway(Protocol):  # 运行网关协议
    """start/events/cancel/approve；UI 只依赖它。"""

    async def start(self, run_input: RunInput) -> str:  # 启动返回 run_id
        """启动一轮，返回 run_id。"""
        ...  # 协议说明

    async def events(  # 订阅事件流
        self, run_id: str, *, last_event_id: str = ""  # run_id / 断线续传点
    ) -> AsyncIterator[Any]:  # 事件流（不含 sentinel）
        """迭代产生该 run 的事件；结束后自然停止。"""
        ...  # 协议说明

    async def cancel(self, run_id: str) -> None:  # 取消
        """请求取消该 run（幂等）。"""
        ...  # 协议说明

    async def approve(self, run_id: str, request_id: str, allow: bool) -> None:  # 审批
        """对补参/审批请求给结论。"""
        ...  # 协议说明
