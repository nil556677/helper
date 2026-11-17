"""SSE 封装：用 sse-starlette，不自研帧/心跳。

- ``to_sse``：事件 → ``ServerSentEvent``（id/event/data/retry）；
- ``sse_response``：生成器 → ``EventSourceResponse``（自动 ping + 反代理缓冲头）。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import AsyncIterator  # 异步迭代
from typing import Any  # 任意值

from sse_starlette.sse import EventSourceResponse, ServerSentEvent  # SSE

from app.agent.contracts import LAST_EVENT_ID_HEADER  # 再导出，供 router 导入

#: 断线续传的读 header / query 用的名字（单点定义在契约层）。


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


#: 心跳间隔（秒）：防代理/空闲断开；sse-starlette 用 ping 发送注释心跳。
_HEARTBEAT_S = 15.0
