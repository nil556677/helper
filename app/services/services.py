"""服务层包装：界面/入口只调这里，不直接 import core 的进程管理。"""

from __future__ import annotations

from typing import Any

from app.core.proc import services as _services


def status() -> dict[str, Any]:
    """Qdrant / worker 的当前状态。"""
    return _services.status()


def start() -> dict[str, Any]:
    """按当前配置拉起服务（幂等）。"""
    return _services.start()


def stop(grace_s: float = 5.0) -> dict[str, str]:
    """停止受管服务。

    返回 ``{name: 状态}``（``stopped / killed / still-running / not-started``）。
    """
    return _services.stop(grace_s=grace_s)


def enable_server(port: int) -> dict[str, Any]:
    """保存服务端模式与端口（重启主程序后生效；不在当前进程起 worker）。"""
    return _services.enable_server(port)


def restart(*, port: int | None = None) -> dict[str, Any]:
    """改端口（可选）→ 重启服务。"""
    return _services.restart(port=port)


def worker_enabled() -> bool:
    """任务是否在独立 worker 进程里跑。"""
    return _services.worker_enabled()


def qdrant_url() -> str:
    """当前 Qdrant 地址（服务端模式；默认 127.0.0.1:6333）。"""
    return _services.qdrant_url()


def qdrant_port() -> int:
    """当前 Qdrant HTTP 端口。"""
    return _services.qdrant_port()


def port_summary(ui_port: int = 0) -> dict[str, Any]:
    """各进程端口汇总（启动日志/排障用）：UI / Qdrant HTTP+gRPC / worker 对话 RPC。"""
    return _services.port_summary(ui_port)


def worker_rpc_port() -> int:
    """worker 的对话 RPC 端口（它写 ``data/worker.port``；未就绪为 0）。"""
    return _services.worker_rpc_port()


def last_cleanup() -> dict[str, list[str]]:
    """上次退出残留子进程的清理结果（``killed`` / ``failed``，启动提示用）。"""
    return _services.last_cleanup()
