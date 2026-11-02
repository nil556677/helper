"""FastAPI 装配：统一中间件、异常处理、lifespan 与路由注册。"""
from __future__ import annotations  # 类型注解延迟求值

from contextlib import asynccontextmanager  # lifespan
from typing import Any  # 任意值

from fastapi import FastAPI, Request  # 框架
from fastapi.exceptions import RequestValidationError  # 校验错误
from fastapi.responses import JSONResponse  # JSON 响应

from app.http import errors  # 错误码
from app.http.deps import get_backend  #（确保导入即注册依赖）— 用作依赖引用
from app.http.errors import ApiError  # 业务异常
from app.http.feeds import ServerBackend  # 门面
from app.http.middleware import request_id_of  # request_id
from app.http.routers import controls, events, health, messages, runs  # 路由

API_PREFIX = "/api/v1"  # API 版本前缀


def make_server(backend: Any, *, on_startup: Any | None = None) -> FastAPI:  # 建 ASGI app
    """以任意 RunGateway 后端构造 HTTP 服务；on_startup 为可选的异步启动钩子。"""
    get_backend  # noqa: B018  # 触发导入（FastAPI 依赖按需 resolve，这里仅确保包已载入）

    @asynccontextmanager  # lifespan
    async def lifespan(_app: FastAPI):  # 进出事件
        if on_startup is not None:  # 有钩子
            await on_startup()  # 于服务 loop 执行
        yield  # 放行

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)  # app
    app.state.backend = ServerBackend(backend)  # 注入门面（deps 从这里取）

    @app.exception_handler(ApiError)  # 业务异常
    async def _api_error(_req: Request, exc: ApiError) -> JSONResponse:  # handler
        return JSONResponse(  # 统一错误体
            status_code=exc.status,  # 状态码
            content={  # 内容
                "code": exc.code,  # 业务码
                "message": exc.message,  # 消息
                "request_id": request_id_of(_req),  # 追踪
                "data": None,  # 数据
            },  # 内容结束
        )  # 返回

    @app.exception_handler(RequestValidationError)  # 校验错误
    async def _validation_error(_req: Request, _exc: RequestValidationError) -> JSONResponse:  # handler
        return JSONResponse(  # 统一错误体
            status_code=422,  # 状态码
            content={  # 内容
                "code": errors.E_BAD_REQUEST,  # 业务码
                "message": "参数错误",  # 消息
                "request_id": request_id_of(_req),  # 追踪
                "data": None,  # 数据
            },  # 内容结束
        )  # 返回

    # 注册路由（统一 /api/v1 前缀）。
    app.include_router(runs.router, prefix=API_PREFIX)  # 消息/运行
    app.include_router(messages.router, prefix=API_PREFIX)  # 历史消息
    app.include_router(events.router, prefix=API_PREFIX)  # SSE
    app.include_router(controls.router, prefix=API_PREFIX)  # 取消/审批
    app.include_router(health.router, prefix=API_PREFIX)  # 健康
    return app  # 返回
