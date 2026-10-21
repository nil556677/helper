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
    """当前 Qdrant 地址（空串 = 本地嵌入式）。"""
    return _services.qdrant_url()


def qdrant_port() -> int:
    """当前 Qdrant HTTP 端口。"""
    return _services.qdrant_port()
