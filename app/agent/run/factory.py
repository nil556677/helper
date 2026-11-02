"""进程级客户端工厂：读 ``data/worker.port`` 返回 ``HttpClient``。

**只走 worker**：没有端口（worker 未就绪）直接抛错，不回退本进程。按端口缓存，
worker 重启换了端口就重建客户端。测试用 :func:`reset_client` 清缓存。
"""
from __future__ import annotations  # 类型注解延迟求值

from pathlib import Path  # 路径

from loguru import logger  # 排障日志

from app.agent.run.gateway import RunGateway  # 协议
from app.agent.run.http_client import HttpClient  # 唯一的远端实现

_CLIENT: RunGateway | None = None  # 模块级缓存
_CLIENT_BASE: str = ""  # 缓存客户端对应的 worker 地址


def get_run_client() -> RunGateway:  # 进程级客户端
    """读 worker.port，返回对应的 HttpClient（无回退）。"""
    global _CLIENT, _CLIENT_BASE  # 改模块级
    port = _read_worker_port()  # 读 worker 端口
    if not port:  # worker 没起
        raise RuntimeError(
            "worker 未就绪（未找到 data/worker.port）。请确认后台 worker 已启动后重试。"
        )  # 明确报错
    base = f"http://127.0.0.1:{port}"  # worker 地址
    if _CLIENT is None or base != _CLIENT_BASE:  # 还没建 / 换了端口
        _CLIENT = HttpClient(base)  # 打 worker（HTTP+SSE）
        _CLIENT_BASE = base  # 记地址
        logger.debug("运行客户端：HTTP worker {}", base)  # 排障
    return _CLIENT  # 返回


def reset_client() -> None:  # 清缓存（测试隔离）
    """置空进程级客户端，让下一次重新读端口。"""
    global _CLIENT, _CLIENT_BASE  # 改模块级
    _CLIENT = None  # 清客户端
    _CLIENT_BASE = ""  # 清地址


def _read_worker_port() -> int:  # 读端口
    from app.core import paths  # 数据目录

    try:  # 读文件
        raw = (Path(paths.data_dir()) / "worker.port").read_text(encoding="utf-8").strip()  # 内容
        return int(raw)  # 端口
    except (OSError, ValueError, TypeError):  # 没有/坏
        return 0  # 无
