"""本机服务（Qdrant 服务端 / 后台 worker）的启动、状态与重启。

## 为什么独立成模块

以前 Qdrant 只在「本地嵌入式」模式跑：UI 与任务同进程，单进程独占目录。
现在走**服务端模式**：UI/worker 都通过 HTTP 连 Qdrant，不再打开本地目录；
服务状态、端口修改与重启需要一个后台入口，界面只读这里的结果。

## 模式（唯一：服务端）

- ``vector.url`` 默认 ``http://127.0.0.1:6333``；helper 启动时拉起
  ``bin/qdrant/qdrant.exe --storage-path data/qdrant-server``；
- UI/worker 都通过 HTTP 连 Qdrant，不再打开本地目录；
- 启动 worker 子进程跑任务，主进程不再跑 Runner；
- 本地嵌入式 ``data/qdrant`` 已移除，不再支持。

## 端口被占

``restart(port)`` 会先把新 ``vector.url`` 写进数据库，再停/起 Qdrant；端口占用时
返回明确错误（界面提示用户换端口），不会把整个应用带崩。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

from app.core import paths
from app.core.config import store as params_store
from app.core.proc.supervisor import ChildSpec, Supervisor, cleanup_stale_children

_supervisor: Supervisor | None = None
#: 主程序**启动时**是否处于服务端模式（决定 Runner 在不在主进程）。
#: 现场从嵌入式切服务端时不能直接起 worker：主进程已有 Runner，会变成两个执行器。
_started_worker = False
#: 串行化 start/stop/restart 与 watchdog（UI 线程和守护线程会同时碰到 Supervisor）。
_LIFECYCLE_LOCK = threading.RLock()
_watchdog: threading.Thread | None = None
_watchdog_stop: threading.Event | None = None
#: 最近一次启动时对"上次残留子进程"的清理结果（供入口提示用户）。
_last_cleanup: dict[str, list[str]] = {"killed": [], "failed": []}


def qdrant_binary() -> Path:
    """随包分发的 qdrant.exe 路径（``bin/qdrant/qdrant.exe``）。"""
    return paths.project_root() / "bin" / "qdrant" / "qdrant.exe"


def service_log_dir() -> Path:
    """受管子进程（Qdrant / worker）的日志目录：**``{安装目录}/logs``**。

    以前误用了 ``data/logs``——日志属于``logs/``（与 helper/console 日志同处一个目录，
    备份/清理只看一个地方），``data/`` 只放数据。
    """
    return paths.logs_dir()


def qdrant_url() -> str:
    """当前配置的 Qdrant 地址；空串 = 本地嵌入式。"""
    try:
        return str(params_store.load()["vector.url"] or "").strip()
    except Exception:
        return ""


def qdrant_port() -> int:
    """从 ``vector.url`` 取 HTTP 端口（默认 6333）。"""
    url = qdrant_url()
    if not url:
        return 6333
    try:
        return int(urlsplit(url).port or 6333)
    except ValueError:
        return 6333


def server_mode() -> bool:
    """是否服务端模式（``vector.url`` 非空）。"""
    return bool(qdrant_url())


def _port_free(host: str, port: int) -> bool:
    """端口能否绑定（只用来提前给"被占用"的明确提示，不替代 Qdrant 自己的报错）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, int(port)))
        except OSError:
            return False
    return True


def _qdrant_ports_free(port: int) -> bool:
    """Qdrant 要两个端口：HTTP=port、gRPC=port+1；两个都空闲才可用。"""
    return 1 <= int(port) <= 65534 and _port_free("127.0.0.1", int(port)) and _port_free(
        "127.0.0.1", int(port) + 1
    )


#: worker 心跳判定阈值（秒）：与 ``app/worker.HEARTBEAT_STALE_S`` 保持一致。
WORKER_HEARTBEAT_STALE_S = 15.0


def _worker_health() -> bool:
    """worker 健康检查：**调度线程还在心跳**才算健康。

    只看"进程还活着"不够——worker 的 Runner 调度线程卡死/退出时进程仍在，但任务会永远
    挂着没有人接管。调度线程每 ≤5s 更新 `data/worker-heartbeat` 文件 mtime，这里判它
    是否新鲜。文件不存在=还没起步（有 60s 起步宽限兜着）或已僵死。
    """
    try:
        age = time.time() - (paths.data_dir() / "worker-heartbeat").stat().st_mtime
    except OSError:
        return False
    return age < WORKER_HEARTBEAT_STALE_S


def _health(port: int | None = None) -> bool:
    """Qdrant 健康检查（/healthz）。不传端口时用配置的 URL（支持远程）。"""
    if port is None:
        url = qdrant_url()
        if not url:
            return False
        try:
            parts = urlsplit(url)
            scheme = parts.scheme or "http"
            host = parts.hostname or "127.0.0.1"
            resolved = parts.port or (443 if scheme == "https" else 6333)
            target = f"{scheme}://{host}:{resolved}/healthz"
        except ValueError:
            return False
    else:
        target = f"http://127.0.0.1:{int(port)}/healthz"
    try:
        headers: dict[str, str] = {}
        api_key = str(params_store.load()["vector.api_key"] or "").strip()
        if api_key:
            # 两条健康检查路径（配置 URL / 本机 sidecar 显式端口）都带 key：
            # 此前显式端口分支不带 key，key 保护的 Qdrant 会被 watchdog 反复误杀重启。
            headers["api-key"] = api_key
        request = urllib.request.Request(target, headers=headers) if headers else target
        with urllib.request.urlopen(request, timeout=0.6) as resp:
            return int(resp.status) == 200
    except Exception:
        return False


def _is_local_url() -> bool:
    """``vector.url`` 是否指向本机（只有本机才由我们拉起 Qdrant sidecar）。"""
    try:
        host = (urlsplit(qdrant_url()).hostname or "").lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _qdrant_spec() -> ChildSpec | None:
    if not _is_local_url():
        return None
    binary = qdrant_binary()
    if not binary.is_file():
        return None
    port = qdrant_port()
    storage = paths.data_dir() / "qdrant-server"
    storage.mkdir(parents=True, exist_ok=True)
    env = {
        "QDRANT__SERVICE__HOST": "127.0.0.1",
        "QDRANT__SERVICE__HTTP_PORT": str(port),
        "QDRANT__SERVICE__GRPC_PORT": str(port + 1),
        "QDRANT__STORAGE__STORAGE_PATH": str(storage),
        # 服务器把每个 HTTP 请求都打 INFO；界面每 2 秒轮询一次会刷爆日志。
        "QDRANT__LOG_LEVEL": "WARN",
    }
    try:
        api_key = str(params_store.load()["vector.api_key"] or "").strip()
    except Exception:
        api_key = ""
    if api_key:
        # 客户端带着 key 连，服务端也得用同一个 key；本地回环也保持一致。
        env["QDRANT__SERVICE__API_KEY"] = api_key
    return ChildSpec(
        name="qdrant",
        argv=[str(binary)],
        cwd=str(binary.parent),
        env=env,
        health=lambda: _health(port),
    )


def _specs() -> list[ChildSpec]:
    specs: list[ChildSpec] = []
    qdrant = _qdrant_spec()
    if _is_local_url() and qdrant is None:
        # 本地服务端模式却没有二进制：不拉起 worker（它只会空等/失败），
        # 让主进程保留 Runner，界面把"未安装"显示出来，用户补文件或改端口。
        logger.error("本地服务端模式缺少 bin/qdrant/qdrant.exe，暂不启动 worker")
        return []
    if qdrant is not None:
        specs.append(qdrant)
    # 服务端模式：任务交给 worker（主进程不跑 Runner）
    specs.append(ChildSpec(name="worker", argv=[sys.executable, "-m", "app.worker"],
                           cwd=str(paths.project_root()), health=_worker_health))
    return specs


def _existing_qdrant_usable() -> bool:
    """端口上的 Qdrant 是否能用当前配置（含 API key）连上。

    只凭 ``/healthz`` 200 不够：可能是别的 HTTP 服务，或另一个 Qdrant 但 key 不同。
    连一次 ``get_collections`` 才能确认"能读"。
    """
    try:
        from app.integrations import qdrant as qi

        qi.qdrant_client().get_collections()
        return True
    except Exception as exc:
        logger.warning("端口上的服务不是当前配置可用的 Qdrant：{}", exc)
        return False


def _ensure_local_port_available() -> None:
    """本地服务端启动前处理端口冲突。

    - 端口空闲：不动；
    - 端口已有**可用** Qdrant（含 API key）：沿用现有实例；
    - 端口被别的程序/不可用 Qdrant 占：自动换到后面的空闲端口并写回数据库。
    """
    if not _is_local_url():
        return
    port = qdrant_port()
    if _qdrant_ports_free(port):
        return
    if _health(port) and _existing_qdrant_usable():
        logger.warning("Qdrant 端口 {} 已有可用服务，沿用现有实例（不再启动第二个）", port)
        return
    for candidate in range(port + 1, port + 21):
        if not _qdrant_ports_free(candidate):
            continue
        params_store.save_values({"vector.url": f"http://127.0.0.1:{candidate}"})
        logger.warning("Qdrant 端口 {} 被占用，已自动换到 {}（可到设置里修改）", port, candidate)
        return
    logger.error("Qdrant 端口 {} 被占用，且后面 20 个端口也不可用", port)


def _watch_loop(stop: threading.Event) -> None:
    """每 5 秒检查一次子进程；崩溃的 Qdrant/worker 自动拉起（有退避上限）。"""
    while not stop.wait(5.0):
        with _LIFECYCLE_LOCK:
            supervisor = _supervisor
            if supervisor is None:
                continue
            try:
                states = supervisor.ensure_running()
            except Exception as exc:
                logger.warning("服务健康检查失败：{}", exc)
                continue
        for name, state in states.items():
            if state != "running":
                logger.warning("服务 {} 状态：{}", name, state)


def _start_watchdog() -> None:
    global _watchdog, _watchdog_stop
    if _watchdog is not None and _watchdog.is_alive():
        return
    stop = threading.Event()
    _watchdog_stop = stop
    _watchdog = threading.Thread(
        target=_watch_loop, args=(stop,), daemon=True, name="service-watchdog"
    )
    _watchdog.start()


def _stop_watchdog() -> None:
    global _watchdog, _watchdog_stop
    if _watchdog_stop is not None:
        _watchdog_stop.set()
    if _watchdog is not None:
        _watchdog.join(timeout=2.0)
    _watchdog = None
    _watchdog_stop = None


def start() -> dict[str, Any]:
    """按当前配置拉起服务（幂等）。返回状态。"""
    global _supervisor, _started_worker, _last_cleanup
    with _LIFECYCLE_LOCK:
        # **先清上次残留**：崩溃/被强杀时 on_quit 不会跑，残留的 Qdrant/worker 会占着端口，
        # 不清掉就会触发"端口被占用自动换端口"甚至连到旧进程，数据写串。
        _last_cleanup = cleanup_stale_children(service_log_dir())
        if _last_cleanup["killed"]:
            logger.warning("已结束上次退出残留的子进程：{}", "、".join(_last_cleanup["killed"]))
        if _last_cleanup["failed"]:
            logger.error("上次退出残留的子进程无法结束：{}", "、".join(_last_cleanup["failed"]))
        _ensure_local_port_available()
        specs = _specs()
        _started_worker = any(spec.name == "worker" for spec in specs)
        if not specs:
            return status()
        if _supervisor is None:
            _supervisor = Supervisor(specs, log_dir=service_log_dir())
        _supervisor.start()
        if any(spec.name == "qdrant" for spec in specs):
            # 等 Qdrant 真正可连再返回：主程序紧接着会建集合；worker 也依赖它。
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not _health(qdrant_port()):
                time.sleep(0.2)
            if not _health(qdrant_port()):
                logger.warning(
                    "Qdrant 在 10s 内没有就绪（端口 {}），检查 {} 下的 qdrant-<日期>.log",
                    qdrant_port(), service_log_dir(),
                )
        _start_watchdog()
        logger.info("本机服务已启动：{}", "、".join(spec.name for spec in specs))
        return status()


def stop(grace_s: float = 5.0) -> dict[str, str]:
    """停止所有受管服务。

    返回 ``{name: 状态}``（``stopped / killed / still-running / not-started``），
    供主进程退出时判断是否有子进程没被正常关掉、需要提醒用户。
    """
    global _supervisor, _started_worker
    report: dict[str, str] = {}
    _stop_watchdog()
    with _LIFECYCLE_LOCK:
        if _supervisor is not None:
            report = _supervisor.stop(grace_s=grace_s)
            _supervisor = None
        _started_worker = False
    return report


def enable_server(port: int) -> dict[str, Any]:
    """保存服务端端口（**不立即启动**）：重启主程序后由入口统一拉起。"""
    port = int(port)
    if not (1 <= port <= 65534):
        return {"ok": False, "message": "端口必须在 1–65534 之间（gRPC 占用 port+1）", **status()}
    if not qdrant_binary().is_file():
        return {
            "ok": False,
            "message": "没有找到 bin/qdrant/qdrant.exe，无法启用 Qdrant 服务端",
            **status(),
        }
    if not _qdrant_ports_free(port):
        return {"ok": False, "message": f"端口 {port}（或 gRPC {port + 1}）已被占用，请换一个端口", **status()}
    params_store.save_values({"vector.url": f"http://127.0.0.1:{port}"})
    return {
        "ok": True,
        "message": f"已保存服务端模式（端口 {port}）；重启 helper 后自动启动 Qdrant 与 worker",
        **status(),
    }


def restart(*, port: int | None = None) -> dict[str, Any]:
    """保存端口（可选）→ 停 → 起；返回状态与一句给人看的结果。

    参数：
        port: 新 HTTP 端口；None 表示不改。
    """
    global _supervisor
    if port is not None:
        port = int(port)
        if not (1 <= port <= 65534):
            return {"ok": False, "message": "端口必须在 1–65534 之间（gRPC 占用 port+1）", **status()}
        host = "127.0.0.1"
        if port != qdrant_port() and not _qdrant_ports_free(port):
            return {
                "ok": False,
                "message": f"端口 {port}（或 gRPC {port + 1}）已被占用，请换一个端口再试",
                **status(),
            }
        params_store.save_values({"vector.url": f"http://{host}:{port}"})
    if server_mode() and not _started_worker:
        # 本次主程序是嵌入式启动的（Runner 在主进程）：只保存，等重启再切。
        return {
            "ok": True,
            "message": f"已保存端口 {qdrant_port()}；重启 helper 后自动启动 Qdrant 与 worker",
            **status(),
        }
    # 停旧服务并**看它的结果**：若有子进程宽限+强杀后仍确认不了退出（still-running），
    # 继续硬启会让新 Qdrant 撞上旧进程还占着的端口。宁可让用户稍后再试，也不拖泥带水。
    stop_report = stop()
    stuck = [
        f"{name}（{state}）" for name, state in stop_report.items() if state == "still-running"
    ]
    if stuck:
        logger.error("重启前旧进程未确认退出：{}", "；".join(stuck))
        return {
            "ok": False,
            "message": "旧后台进程未能在宽限内确认退出：" + "；".join(stuck)
            + "。已尽力强制清理，请稍后重试或到任务管理器手动结束后再开。",
            **status(),
        }
    _supervisor = None
    result = start()
    qdrant = result.get("qdrant", {})
    if qdrant.get("installed") is False:
        return {
            "ok": False,
            "message": "没有找到 bin/qdrant/qdrant.exe，无法启动 Qdrant 服务端",
            **result,
        }
    if not qdrant.get("running"):
        return {
            "ok": False,
            "message": f"Qdrant 启动失败（端口 {qdrant_port()} 是否被占用？详情见 {service_log_dir()} 下的 qdrant-<日期>.log）",
            **result,
        }
    return {"ok": True, "message": f"Qdrant 已在端口 {qdrant_port()} 启动（服务端模式）", **result}


def worker_enabled() -> bool:
    """是否由 worker 子进程跑任务（``start()`` 时实际拉起 worker 才为真）。"""
    return _started_worker


def last_cleanup() -> dict[str, list[str]]:
    """最近一次启动时对上次残留子进程的清理结果（``killed`` / ``failed``）。"""
    return _last_cleanup


def status() -> dict[str, Any]:
    """服务状态（界面直接展示）。"""
    installed = qdrant_binary().is_file()
    children: dict[str, dict[str, Any]] = {}
    # 与看门狗/start/stop 串行：`_supervisor.status()` 会读 `child.proc`，而它们会在
    # 重启时替换该引用——不加锁会读到换到一半的瞬时状态（旧 pid/旧重启计数）。
    with _LIFECYCLE_LOCK:
        if _supervisor is not None:
            children = _supervisor.status()
    qdrant_child = children.get("qdrant", {})
    worker_child = children.get("worker", {})
    port = qdrant_port()
    health = _health() if server_mode() else False
    return {
        "mode": "server",
        "url": qdrant_url(),
        "port": port,
        "qdrant": {
            "installed": installed,
            "local": _is_local_url(),
            "running": bool(qdrant_child.get("running")) or health,
            "pid": qdrant_child.get("pid"),
            "degraded": bool(qdrant_child.get("degraded")),
            "health": health,
        },
        "worker": {
            "running": bool(worker_child.get("running")),
            "pid": worker_child.get("pid"),
            "degraded": bool(worker_child.get("degraded")),
        },
    }


def worker_rpc_port() -> int:
    """worker 的对话 RPC 端口（它自己写 ``data/worker.port``）；没有/坏了返回 0。"""
    try:  # 读端口文件
        return int((paths.data_dir() / "worker.port").read_text(encoding="utf-8").strip())  # 端口
    except (OSError, ValueError, TypeError):  # 文件缺失 / 内容非数字
        return 0  # 未就绪


def port_summary(ui_port: int = 0) -> dict[str, Any]:
    """汇总本次启动各进程的端口（启动日志/排障用，一眼看清谁在哪）。

    返回字段：``ui``（NiceGUI 页面端口，窗口加载的就是它）、``qdrant_http`` /
    ``qdrant_grpc``（本机 sidecar；非本机/无配置为 0）、``worker_rpc``（worker 对话
    RPC，未就绪为 0）、``qdrant_pid`` / ``worker_pid``（子进程 pid，没跑为 None）。
    """
    st = status()  # 当前服务状态（跑没跑、pid）
    qdrant = st.get("qdrant") or {}  # Qdrant 段
    worker = st.get("worker") or {}  # worker 段
    http_port = int(st.get("port") or 0) if (qdrant.get("local") and st.get("url")) else 0  # 本机 HTTP 口
    return {  # 汇总
        "ui": int(ui_port),  # UI 服务端口
        "qdrant_http": http_port,  # Qdrant HTTP
        "qdrant_grpc": http_port + 1 if http_port else 0,  # Qdrant gRPC（固定占 HTTP+1）
        "worker_rpc": worker_rpc_port(),  # worker 对话 RPC
        "qdrant_pid": qdrant.get("pid"),  # Qdrant pid
        "worker_pid": worker.get("pid"),  # worker pid
    }
