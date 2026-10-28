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


def _serve_run_rpc() -> None:  # 启动对话 RPC 服务（E0.2）
    """在守护线程跑 uvicorn，监听 127.0.0.1 动态端口，端口写给 data/worker.port。

    后端是本进程的 ``InProcessClient``（真实业务）；UI 用 ``HttpClient`` 经此调用。
    """
    try:  # 失败只告警，不让 worker 因它崩
        from app.agent.run_client import InProcessClient
        from app.agent.run_server import serve_in_thread
        from app.agent.worker_cp import setup_loop
        from app.core import paths

        _server, _port = serve_in_thread(  # 起服务
            InProcessClient(),  # 本进程后端
            port_file=str(paths.data_dir() / "worker.port"),  # 端口文件
            startup=setup_loop,  # 服务 loop 里建 checkpointed 图 + 对账（E2/E5）
        )  # 服务结束
        from loguru import logger

        logger.info("对话 RPC 服务已启动：127.0.0.1:{}", _port)  # 日志
    except Exception as exc:  # 启动失败
        from loguru import logger

        logger.opt(exception=exc).warning("对话 RPC 服务启动失败（不影响任务执行）")  # 告警


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


#: 心跳判定阈值（秒）：与服务端 ``services.WORKER_HEARTBEAT_STALE_S`` 保持一致。
#: 调度线程 ≤5s 打一次，15s 的阈值留够判定余量，又能在调度线程序号真卡死时及时超时。
#: 服务端那边不 import 本模块（这里是 worker 自己的进程，import 会重复拉起逻辑），
#: 所以这个值在两个地方各写一份、靠注释钉住一致。
HEARTBEAT_STALE_S = 15.0


def _touch_heartbeat() -> None:
    """更新 worker 心跳文件 mtime（挂在 Runner 调度线程上打）。

    为什么挂在调度线程而不是 worker 主循环：主循环只 ``time.sleep``，任务其实跑在
    Runner 的工作线程里；真正决定"进程还活着却在不在干活"的是**调度线程**——它认领、
    续租、扫地回收，它停摆时任务会在库里永远挂着 RUNNING 没人接管。心跳挂在调度线程
    上，一旦它卡死/退出，主进程看门狗就能据此判定不健康并重启 worker（新 worker 会
    ``recover_on_start`` 把这些悬空任务收尸回队列）。
    """
    from app.core import paths

    target = paths.data_dir() / "worker-heartbeat"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)  # 更新 mtime = now
    except Exception:
        pass  # 心跳写失败不致命，主进程最多误判一次重启


def main() -> int:
    """worker 主循环：初始化 DB + 启动 Runner，然后阻塞直到收到停止信号。"""
    multiprocessing.freeze_support()  # PyInstaller 打包必需

    from app.core.logging.setup import init_child_logging  # 子进程日志初始化

    # 与主日志同一套格式（含 run / 模块:行）；只装 stderr sink——它的 stderr 已由
    # Supervisor 重定向到 ``logs/worker-<日期>.log``，再加文件 sink 就会双写。
    init_child_logging("worker")

    from loguru import logger

    try:
        from app.core.db.schema import init_db

        init_db()
        _wait_qdrant()
        from app import tasks as tasks_app

        tasks_app.start(heartbeat=_touch_heartbeat)
        _serve_run_rpc()  # 拉起对话 RPC 服务（E0.2）
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
        # 关掉 checkpoint 的 aiosqlite 连接（尽力而为）：不关会在磁盘上留
        # ``checkpoints.sqlite-wal/-shm``。worker 常被强制结束，所以这里只是"能关就关"。
        with contextlib.suppress(Exception):
            import asyncio as _asyncio

            from app.agent.checkpoint import close_async_savers

            _asyncio.run(close_async_savers())
        with contextlib.suppress(Exception):
            tasks_app.stop(grace_s=3.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
