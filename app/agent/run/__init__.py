"""运行网关包：UI 与服务端共用的运行接口与实现。

- :mod:`.types`：`RunInput` / `SENTINEL`（DTO）；
- :mod:`.gateway`：`RunGateway` 协议（start/events/cancel/approve）；
- :mod:`.local`：`LocalBackend`（worker 内部后端）；
- :mod:`.http_client`：`HttpClient`（UI 使用，打 worker）；
- :mod:`.session`：`RunSession`（UI 句柄 + 断线续传）；
- :mod:`.factory`：`get_run_client`（读 worker.port）。
"""
from __future__ import annotations  # 类型注解延迟求值

from app.agent.run.factory import get_run_client, reset_client  # 工厂
from app.agent.run.gateway import RunGateway  # 协议
from app.agent.run.http_client import HttpClient  # 客户端
from app.agent.run.local import LocalBackend  # 后端
from app.agent.run.session import RunSession, _split_event  # 会话
from app.agent.run.types import SENTINEL, RunInput  # DTO

__all__ = [  # 对外导出
    "SENTINEL",  # 结束标记
    "HttpClient",  # 客户端
    "LocalBackend",  # 后端
    "RunGateway",  # 协议
    "RunInput",  # 入参
    "RunSession",  # 会话
    "_split_event",  # 归一化
    "get_run_client",  # 工厂
    "reset_client",  # 清缓存
]
