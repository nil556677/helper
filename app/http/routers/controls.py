"""路由：取消 / 审批，原样转发给后端。"""
from __future__ import annotations  # 类型注解延迟求值

from fastapi import APIRouter, Depends, Request  # 路由/依赖/请求

from app.http.deps import get_backend  # 依赖注入
from app.http.feeds import ServerBackend  # 门面
from app.http.middleware import request_id_of  # request_id
from app.http.schemas import ApproveRequest, ResumeRequest, ok  # DTO/成功体

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


@router.post("/runs/{run_id}/resume", status_code=202)  # HITL 恢复
async def resume_run(  # 恢复
    run_id: str,  # run id
    body: ResumeRequest,  # 恢复体
    request: Request,  # 请求
    backend: ServerBackend = Depends(get_backend),  # 门面
) -> dict:  # envelope
    """用用户选择恢复被暂停的运行（澄清卡点选后调用）。"""
    await backend.backend.resume(run_id, body.value)  # 转发
    return ok({"ok": True}, request_id=request_id_of(request))  # 成功体
