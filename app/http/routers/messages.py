"""路由：消息资源（`/api/v1/conversations/{cid}/messages`）。"""
from __future__ import annotations  # 类型注解延迟求值

from fastapi import APIRouter, Depends, Query, Request  # 依赖/查询/请求

from app.http import errors  # 错误码
from app.http.deps import get_messages_repo  # 依赖注入
from app.http.errors import ApiError  # 业务异常
from app.http.middleware import request_id_of  # request_id
from app.http.repositories.messages import MAX_LIMIT, MessageRepository  # 仓储
from app.http.schemas import ok  # 成功体

router = APIRouter(tags=["messages"])  # 路由


@router.get("/conversations/{cid}/messages")  # 历史消息（分页）
async def list_messages(  # 列表
    cid: str,  # 会话 id
    request: Request,  # 请求
    before: int | None = Query(default=None),  # 游标（最旧消息 id）
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),  # 每页条数
    repo: MessageRepository = Depends(get_messages_repo),  # 仓储
) -> dict:  # envelope
    """按 seq 倒序分页取会话的 chat 消息，返回 items + next_cursor。"""
    data = await repo.list(cid, before=before, limit=limit)  # 查询
    return ok(data, request_id=request_id_of(request))  # 成功体


@router.get("/conversations/{cid}/messages/{mid}")  # 单条详情
async def get_message(  # 详情
    cid: str,  # 会话 id
    mid: int,  # 消息 id
    request: Request,  # 请求
    repo: MessageRepository = Depends(get_messages_repo),  # 仓储
) -> dict:  # envelope
    """取一条消息（含 assistant 的 turn_json 供历史复现）。"""
    row = await repo.get(cid, mid)  # 查询
    if row is None:  # 不存在
        raise ApiError(errors.E_NOT_FOUND, "消息不存在", 404)  # 404
    return ok(row, request_id=request_id_of(request))  # 成功体
