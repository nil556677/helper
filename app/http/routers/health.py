"""路由：健康检查。"""
from __future__ import annotations  # 类型注解延迟求值

from fastapi import APIRouter, Depends, Request  # 路由/依赖/请求

from app.http.deps import get_backend  # 依赖注入
from app.http.feeds import ServerBackend  # 门面
from app.http.middleware import request_id_of  # request_id
from app.http.schemas import ok  # 成功体

router = APIRouter(tags=["health"])  # 路由


@router.get("/health")  # 健康检查
async def health(  # 健康
    request: Request,  # 请求
    backend: ServerBackend = Depends(get_backend),  # 门面
) -> dict:  # envelope
    """返回服务是否就绪 + 当前活动 run/feed 数（排障用）。"""
    return ok(  # 成功体
        {"ok": True, "active_runs": len(backend.feeds)},  # 数据
        request_id=request_id_of(request),  # 追踪
    )  # 结束
