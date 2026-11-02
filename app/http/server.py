"""服务启动：在守护线程里起 uvicorn（供 worker 使用）。"""
from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from app.http.feeds import ServerBackend  # 门面（类型提示）


def serve_in_thread(  # 起服务
    backend: Any,  # 后端（RunGateway 实现）
    *,  # 仅关键字
    port_file: str = "",  # 端口文件
    startup: Any | None = None,  # 启动钩子（async，见 make_server）
) -> tuple[Any, int]:
    """在守护线程里起 uvicorn，监听 127.0.0.1 动态端口；把端口写给 port_file（可选）。

    返回：``(server, port)``，调用方设 ``server.should_exit = True`` 可停。
    供 worker 进程使用。
    """
    import socket  # 找空闲端口
    import threading  # 线程

    import uvicorn  # ASGI 服务

    from app.http.app import make_server  # 建 ASGI app

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:  # 占一个空闲口
        probe.bind(("127.0.0.1", 0))  # 绑定
        port = int(probe.getsockname()[1])  # 拿到端口
    config = uvicorn.Config(  # 配置
        make_server(backend, on_startup=startup),  # app（带启动钩子）
        host="127.0.0.1",  # 只监听本地
        port=port,  # 动态端口
        log_level="warning",  # 少日志
        lifespan="on",  # 开 lifespan，跑启动钩子
    )  # 配置结束
    server = uvicorn.Server(config)  # 服务
    thread = threading.Thread(target=server.run, daemon=True, name="run-rpc")  # 守护线程
    thread.start()  # 起
    if port_file:  # 要写端口文件
        from pathlib import Path  # 路径

        target = Path(port_file)  # 文件
        target.parent.mkdir(parents=True, exist_ok=True)  # 建目录
        target.write_text(str(port), encoding="utf-8")  # 写端口
    return server, port  # 返回


__all__ = ["ServerBackend", "serve_in_thread"]  # 再导出 ServerBackend 便于引用
