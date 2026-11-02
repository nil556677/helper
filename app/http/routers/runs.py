"""路由：创建一轮（发送消息）与查询一轮状态。"""
from __future__ import annotations  # 类型注解延迟求值

import uuid  # 先生成 run_id

from fastapi import APIRouter, Depends, Request  # 依赖/请求

from app.agent.run.types import RunInput  # 入参
from app.http import errors  # 错误码
from app.http.deps import get_backend, get_messages_repo, get_runs_repo  # 依赖注入
from app.http.errors import ApiError  # 业务异常
from app.http.feeds import ServerBackend  # 门面
from app.http.middleware import request_id_of  # request_id
from app.http.repositories.messages import MessageRepository  # 消息仓储
from app.http.repositories.runs import RunRepository  # 运行状态仓储
from app.http.schemas import RunCreateRequest, ok  # DTO/成功体

router = APIRouter(tags=["runs"])  # 路由


@router.post("/runs", status_code=201)  # 发送消息（建一轮）
async def create_run(  # 建 run
    payload: RunCreateRequest,  # 请求体
    request: Request,  # 请求
    backend: ServerBackend = Depends(get_backend),  # 门面
    messages: MessageRepository = Depends(get_messages_repo),  # 消息仓储
) -> dict:  # envelope
    """启动一轮，并发送即落库写一条进行中的 user 消息，返回 run_id + message_id。

    顺序很重要：**先生成 run_id → 先写 user 并提交 → 再启动 agent**。
    这样写 user 与 agent 的同步写不重叠，避免 ``database is locked``；
    且两边用同一 run_id，收尾去重能命中。
    """
    run_id = uuid.uuid4().hex  # 先生成 run id
    message_id = await messages.insert_user(  # 发送即落库（异步写 user，先提交）
        payload.conversation_id,  # 会话
        payload.question,  # 问题
        run_id,  # run id
    )  # 落 user（已提交）
    run_input = RunInput(  # 组入参
        question=payload.question,  # 问题
        conversation_id=payload.conversation_id,  # 会话
        attachments=list(payload.attachments),  # 附件
        run_id=run_id,  # 复用同一 run id
    )  # 结束
    started = await backend.create(run_input)  # 再启动（复用同一 run_id）
    return ok(  # 成功体
        {"run_id": started, "message_id": message_id},  # 数据
        request_id=request_id_of(request),  # 追踪
    )  # 结束


@router.get("/runs/{run_id}")  # 查询一轮状态
async def get_run(  # 状态
    run_id: str,  # run id
    request: Request,  # 请求
    repo: RunRepository = Depends(get_runs_repo),  # 仓储
) -> dict:  # envelope
    """取一轮的状态/结果（非流一次拉取）。"""
    row = await repo.status(run_id)  # 查询
    if row is None:  # 不存在
        raise ApiError(errors.E_RUN_NOT_FOUND, "run 不存在", 404)  # 404
    return ok(row, request_id=request_id_of(request))  # 成功体
