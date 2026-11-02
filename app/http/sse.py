"""SSE 封装：用 sse-starlette，不自研帧/心跳。

- ``to_sse``：事件 → ``ServerSentEvent``（id/event/data/retry）；
- ``sse_response``：生成器 → ``EventSourceResponse``（自动 ping + 反代理缓冲头）。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import AsyncIterator  # 异步迭代
from typing import Any  # 任意值

from sse_starlette.sse import EventSourceResponse, ServerSentEvent  # SSE

#: 断线续传的读 header / query 用的名字。
LAST_EVENT_ID_HEADER = "Last-Event-ID"


def to_sse(seq: int, event: Any) -> ServerSentEvent:  # 事件转帧
    """带 id（续传点）、event（类型名）、data（事件体）、retry（重连间隔）。"""
    return ServerSentEvent(  # 构造
        id=str(seq),  # 续传点
        event=str(event.type),  # 命名事件
        data=event.model_dump_json(),  # 事件体
        retry=3000,  # 客户端重连间隔（ms）
    )  # 结束


def sse_response(generator: AsyncIterator[ServerSentEvent]) -> EventSourceResponse:  # 响应
    """包成 EventSourceResponse；ping=15s 由库自动发注释心跳。"""
    return EventSourceResponse(  # 构造
        generator,  # 生成器
        ping=_HEARTBEAT_S,  # 心跳（秒）
        headers={  # 头
            "Cache-Control": "no-cache",  # 不缓存
            "Connection": "keep-alive",  # 长连
            "X-Accel-Buffering": "no",  # 防 nginx 代理缓冲
        },  # 头结束
    )  # 结束


#: 心跳间隔，与 feeds._HEARTBEAT_S 对齐（sse-starlette 使用）。
_HEARTBEAT_S = 15.0
