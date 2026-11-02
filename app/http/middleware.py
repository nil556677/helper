"""request_id 生成与贯穿（轻量依赖，不用 BaseHTTPMiddleware）。

> 为什么不用 ``BaseHTTPMiddleware``：它会包裹整个响应，对 SSE（``StreamingResponse``）
> 可能缓冲响应体、破坏逐帧推送。这里改为**在路由内按需取**：首取时生成并缓存到
> ``request.state``，之后同请求复用。业务逻辑（PII/限流等）在 agent 侧，本模块只管传输横切。
"""
from __future__ import annotations  # 类型注解延迟求值

import uuid  # request_id

from starlette.requests import Request  # 请求

#: 存 request_id 的 Request.state 键名。
REQUEST_ID_STATE = "request_id"


def request_id_of(request: Request) -> str:  # 取/生成 request_id
    """同一请求内幂等：首取生成，之后复用；可外部 X-Request-Id 带入。"""
    rid = getattr(request.state, REQUEST_ID_STATE, "")  # 取缓存
    if rid:  # 已有
        return str(rid)  # 返回
    rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]  # 外部带入或生成
    setattr(request.state, REQUEST_ID_STATE, rid)  # 缓存
    return str(rid)  # 返回
