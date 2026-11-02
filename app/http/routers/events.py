"""路由：SSE 事件流（取消息）。"""
from __future__ import annotations  # 类型注解延迟求值

from fastapi import APIRouter, Depends, Request  # 路由/依赖/请求

from app.http import errors  # 错误码
from app.http.deps import get_backend  # 依赖注入
from app.http.errors import ApiError  # 业务异常
from app.http.feeds import ServerBackend  # 门面
from app.http.sse import LAST_EVENT_ID_HEADER, sse_response, to_sse  # SSE

router = APIRouter(tags=["events"])  # 路由


@router.get("/runs/{run_id}/events")  # SSE 订阅
async def stream_events(  # 事件流
    run_id: str,  # run id
    request: Request,  # 请求（读 Last-Event-ID）
    backend: ServerBackend = Depends(get_backend),  # 门面
):
    """按 Last-Event-ID 补发事件；sse-starlette 负责心跳与断连。"""
    feed = backend.feed(run_id)  # 找 feed
    if feed is None:  # 不存在
        raise ApiError(errors.E_RUN_NOT_FOUND, "run 不存在", 404)  # 404
    last = _last_event_id(request)  # 续传点

    async def gen():  # SSE 生成器
        seen = last  # 已发到的序号
        while True:  # 长连接
            for seq, event in feed.emit_pending(seen):  # 新事件
                seen = seq  # 更新
                yield to_sse(seq, event)  # 帧
            if feed.done:  # 已消费完
                break  # 关流
            await feed.wait_cleared(seen)  # 等新数据（心跳由库负责）

    return sse_response(gen())  # 返回 SSE


def _last_event_id(request: Request) -> int:  # 取续传点
    """优先 header，其次 query（等价于旧的 run_server._last_event_id）。"""
    raw = request.headers.get(LAST_EVENT_ID_HEADER) or request.query_params.get("last_event_id") or ""  # 取
    try:  # 转数字
        return int(raw)  # 返回
    except (TypeError, ValueError):  # 非数字
        return 0  # 从头
