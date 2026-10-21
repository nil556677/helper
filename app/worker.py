"""独立 worker 进程入口（P7）。

主进程把任务执行搬到这个子进程里跑：
- 复用现有 ``Runner``（cpu/io 泳道、租约、cursor、取消/暂停）；
- 不跑 NiceGUI，不加载界面库；embedding 模型只在这个进程里加载；
- 崩溃后由主进程守护重启（见 ``app.core.proc.Supervisor``）。

启动：``python -m app.worker``（PyInstaller 下由主进程以子进程方式拉起）。

与主进程的协作：共享 SQLite ``jobs`` 表（claim/lease），**无 IPC**（轮询）。
"""

from __future__ import annotations

import contextlib
import multiprocessing
import time


def _wait_qdrant(timeout_s: float = 30.0) -> None:
    """服务端模式下等 Qdrant 就绪再跑任务（Supervisor 同时拉起两者）。"""
    from urllib.parse import urlsplit
    from urllib.request import Request, urlopen

    from app.core.config import store as params_store

    try:
        config = params_store.load()
        url = str(config["vector.url"] or "").strip()
    except Exception:
        return
    if not url:
        return
    try:
        parts = urlsplit(url)
        scheme = parts.scheme or "http"
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if scheme == "https" else 6333)
        health = f"{scheme}://{host}:{port}/healthz"
        api_key = str(config["vector.api_key"] or "").strip()
    except ValueError:
        return
    headers = {"api-key": api_key} if api_key else {}
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(health, headers=headers), timeout=0.6) as resp:
                if int(resp.status) == 200:
                    return
        except Exception:
            pass
        time.sleep(0.5)


def main() -> int:
    """worker 主循环：初始化 DB + 启动 Runner，然后阻塞直到收到停止信号。"""
    multiprocessing.freeze_support()  # PyInstaller 打包必需

    from loguru import logger

    try:
        from app.core.db.schema import init_db

        init_db()
        _wait_qdrant()
        from app import tasks as tasks_app

        tasks_app.start()
    except Exception as exc:
        logger.exception("worker 初始化失败：{}", exc)
        raise
    try:
        while True:
            time.sleep(1.0)
            # 主进程退出时会 terminate 本进程；这里只做长驻。
            # 如需"收到某标记就退出"，可在此检查文件/信号。
    except KeyboardInterrupt:
        pass
    finally:
        with contextlib.suppress(Exception):
            tasks_app.stop(grace_s=3.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
