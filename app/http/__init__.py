"""HTTP 传输层（worker 的 FastAPI 服务端）。

- `.errors`：`ApiError` 业务异常 + 统一错误码；
- `.schemas`：统一 `ApiEnvelope`（code/message/request_id/data）；
- `.feeds`：事件缓冲 `Feed` / `ServerBackend`；
- `.sse`：sse-starlette 封装；
- `.deps`：依赖注入；
- `.middleware`：request_id / 日志；
- `.app`：`create_app()` 装配；
- `.server`：`serve_in_thread()`（uvicorn 启动）；
- `.routers`：按资源拆分的路由。
"""
from __future__ import annotations  # 类型注解延迟求值
