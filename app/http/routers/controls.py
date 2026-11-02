"""路由：取消 / 审批，原样转发给后端。"""
from __future__ import annotations  # 类型注解延迟求值

from fastapi import APIRouter, Depends, Request  # 路由/依赖/请求

from app.http.deps import get_backend  # 依赖注入
from app.http.feeds import ServerBackend  # 门面
from app.http.middleware import request_id_of  # request_id
from app.http.schemas import ApproveRequest, ok  # DTO/成功体

router = APIRouter(tags=["controls"])  # 路由


@router.post("/runs/{run_id}/cancel", status_code=202)  # 取消
async def cancel_run(  # 取消
    run_id: str,  # run id
    request: Request,  # 请求
    backend: ServerBackend = Depends(get_backend),  # 门面
) -> dict:  # envelope
    """请求取消一轮（幂等）。"""
    await backend.backend.cancel(run_id)  # 转发
    return ok({"ok": True}, request_id=request_id_of(request))  # 成功体


@router.post("/runs/{run_id}/approve", status_code=202)  # 审批
async def approve_run(  # 审批
    run_id: str,  # run id
    body: ApproveRequest,  # 审批体
    request: Request,  # 请求
    backend: ServerBackend = Depends(get_backend),  # 门面
) -> dict:  # envelope
    """对一次补参/审批请求给结论。"""
    await backend.backend.approve(run_id, body.request_id, body.allow)  # 转发
    return ok({"ok": True}, request_id=request_id_of(request))  # 成功体
