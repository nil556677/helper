"""子进程守护：拉起 / 健康检查 / 退避重启 / 优雅停止。

Qdrant sidecar 与独立 worker 都复用它。**主进程是唯一守护者**：
- 子进程崩溃 → 按指数退避重启；短时间内反复崩溃 → 放弃并标降级；
- 退出 → 先请求子进程 terminate，宽限后 kill；
- Windows 用 ``CREATE_NO_WINDOW`` 避免黑框。

Job Object（主进程被强杀时连带杀子进程）见 :func:`_create_job_object`。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

#: 健康检查连续失败多少次才判"不健康"并终止重启。看门狗每 5 秒探一次，3 次 ≈ 15 秒，
#: 够扛过一次瞬时繁忙（Qdrant 索引压力大时 /healthz 可能短暂超时），又不会让真故障拖太久。
HEALTH_FAIL_LIMIT = 3

#: 进程刚拉起后的"起步宽限"（秒）：子进程初始化、等依赖就绪这段时间不参与健康检查计数。
#: 没有它，worker 等 Qdrant 就绪（最多 30s）或加载模型时会被误判"不健康"反复杀掉重启。
HEALTH_START_GRACE_S = 60.0

#: 子进程登记文件名（放在 ``log_dir`` 的父目录，即 ``data/children.json``）。
#: 用途：主进程被强杀/崩溃时 ``stop()`` 来不及跑，下次启动靠它把残留子进程清掉。
CHILD_REGISTRY_NAME = "children.json"

#: 关闭 Job 之后，单个子进程还赖着不走的确认窗口（秒）；超时才判 still-running。
STOP_CONFIRM_S = 2.0

#: 整树强杀（taskkill /T /F）之后，再等它消失的窗口（秒）。
STOP_FORCE_WAIT_S = 3.0


@dataclass
class ChildSpec:
    """一个受管子进程的声明。"""

    name: str
    argv: list[str]
    #: 健康检查（默认判"进程活着"）。返回 False 视为不健康，会重启。
    health: Callable[[], bool] | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None


@dataclass
class _Child:
    spec: ChildSpec
    proc: subprocess.Popen | None = None
    restarts: int = 0
    last_start: float = 0.0
    degraded: bool = False
    log: Any = None
    #: 连续健康检查失败次数（去抖用，见 :data:`HEALTH_FAIL_LIMIT`）。
    health_fails: int = field(default=0)
    #: 最近一次成功拉起/重启的墙上时钟（界面可据此展示"多久前崩过重拉"）。
    restarted_at: float = field(default=0.0)


class Supervisor:
    """守护一组子进程。"""

    def __init__(
        self,
        specs: list[ChildSpec],
        *,
        log_dir: Path | None = None,
        max_restarts: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
    ) -> None:
        self._children = [_Child(spec=spec) for spec in specs]
        self.log_dir = log_dir
        #: 子进程登记表（``data/children.json``）：崩溃/被强杀后靠它做下次启动清理。
        self._registry: Path | None = child_registry_path(log_dir)
        self.max_restarts = int(max_restarts)
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        #: Windows Job Object：主进程被强杀/崩溃时，内核连带结束 Qdrant/worker，
        #: 不留下孤儿服务进程（见 :func:`_create_job_object`）。
        self._job = _create_job_object()

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """拉起所有子进程（幂等：已在跑的跳过）。"""
        for child in self._children:
            if child.proc is not None and child.proc.poll() is None:
                continue
            self._spawn(child)

    def ensure_running(self) -> dict[str, str]:
        """检查并重启已退出的子进程；返回 ``{name: state}``。

        state ∈ ``running / restarted / degraded``。
        """
        states: dict[str, str] = {}
        for child in self._children:
            if child.degraded:
                states[child.spec.name] = "degraded"
                continue
            proc = child.proc
            if proc is not None and proc.poll() is None:
                # 起步宽限：刚拉起的进程先不判健康（初始化/等依赖/worker 等 qdrant），
                # 否则会被误杀重启，永远到不了就绪。
                if time.monotonic() - child.last_start < HEALTH_START_GRACE_S:
                    states[child.spec.name] = "running"
                    continue
                if child.spec.health is None or _safe_health(child.spec.health):
                    child.health_fails = 0  # 健康恢复：清零连续失败计数
                    states[child.spec.name] = "running"
                    continue
                # 健康检查失败：**去抖**——连续失败超过阈值才判不健康并终止。
                # Qdrant/worker 瞬时繁忙（索引写入、gc）时 /healthz 可能一次超时，
                # 直接杀会误伤一个健康的子进程。累计连续失败，跨过阈值才走终止/重启。
                child.health_fails += 1
                logger.warning(
                    "子进程 {} 健康检查失败（第 {}/{} 次，进程仍存活）",
                    child.spec.name, child.health_fails, HEALTH_FAIL_LIMIT,
                )
                if child.health_fails < HEALTH_FAIL_LIMIT:
                    states[child.spec.name] = "running"  # 还没到阈值：进程活着，先不动它
                    continue
                _terminate(proc)
            # 崩溃或未启动：退避后重启
            if time.monotonic() - child.last_start < self._backoff(child.restarts):
                states[child.spec.name] = "degraded"
                continue
            child.restarts += 1
            if child.restarts > self.max_restarts:
                child.degraded = True
                logger.error("子进程反复崩溃，停止重启：{}", child.spec.name)
                states[child.spec.name] = "degraded"
                continue
            self._spawn(child)
            states[child.spec.name] = "restarted"
        return states

    def status(self) -> dict[str, dict[str, Any]]:
        """各子进程的当前状态（界面「服务」卡用）：运行中/pid/重启次数/降级。"""
        out: dict[str, dict[str, Any]] = {}
        for child in self._children:
            proc = child.proc
            running = proc is not None and proc.poll() is None
            out[child.spec.name] = {
                "running": running,
                "alive": running,
                "pid": getattr(proc, "pid", None) if running else None,
                "restarts": child.restarts,
                "degraded": child.degraded,
                # 最近一次拉起/重启时刻（0 = 本次运行还没有拉起记录）。
                "restarted_at": child.restarted_at or None,
            }
        return out

    def stop(self, grace_s: float = 3.0) -> dict[str, str]:
        """请求子进程收尾（terminate），宽限后 kill，**并等每个子进程真正退出**。

        **逆序停**：specs 是"先启动的在前"（如 Qdrant → worker），退出时必须反着来，
        先停 worker 再停 Qdrant，否则 worker 还在写、Qdrant 已经没了。

        **退出要等其他子进程先关掉**：主进程关窗前必须确认所有受管子进程都已退出，
        否则会留下孤儿进程（占端口、或还在写数据）。因此这里对每个 child 都先
        ``_terminate``（宽限等待）→ 仍没死就 kill → 再给一小段确认窗口。Job Object
        的 KILL_ON_JOB_CLOSE 只作最后兜底，不能替代这里的显式等待。

        返回：``{name: 状态}``，状态 ∈ ``stopped / killed / still-running / not-started``。
        调用方（主进程退出逻辑）据此判断是否需要提醒用户。
        """
        report: dict[str, str] = {}
        # **退出要等其他子进程先关掉**：先统一发出 terminate（逆序），缩短总退出时间，
        # 再逐个等待；宽限没过就 kill，最后给一小段确认窗口。
        for child in reversed(self._children):
            proc = child.proc
            if proc is None or proc.poll() is not None:
                report[child.spec.name] = "not-started"
                continue
            with contextlib.suppress(OSError):
                proc.terminate()
            if child.log is not None:
                with contextlib.suppress(Exception):
                    child.log.close()
        # 等每个子进程退出；宽限没过就 kill。
        for child in reversed(self._children):
            name = child.spec.name
            if report.get(name) == "not-started":
                continue
            report[name] = _terminate(child.proc, grace_s=grace_s)
        # 关闭 Job 句柄会连带杀掉仍未退出的子进程（正常路径已先 terminate）。
        job, self._job = self._job, None
        if job is not None:
            _close_job(job)
        # 最后确认：Job 关闭后仍活着的，整树强杀再确认一次；还活着才是真正的异常。
        for child in self._children:
            name = child.spec.name
            if report.get(name) == "not-started":
                self._unregister(child)  # 早已退出：登记表也摘掉
                continue
            proc = child.proc
            if proc is None:
                continue
            if not _wait_exit(proc, STOP_CONFIRM_S):  # 宽限内没走
                logger.warning("子进程未在宽限内退出，强制结束进程树：{}（pid {}）", name, proc.pid)
                _kill_pid_tree(proc.pid)  # taskkill /T /F：连子孙一起
                if not _wait_exit(proc, STOP_FORCE_WAIT_S):  # 强杀后仍不退
                    logger.error("子进程在停止后仍未退出：{}（pid {}）", name, proc.pid)
                    report[name] = "still-running"  # 留给上层提示用户
                    continue  # 登记表保留，下次启动继续清
                report[name] = "killed"
            self._unregister(child)  # 确认已退出
        return report

    def pids(self) -> dict[str, int | None]:
        return {c.spec.name: (c.proc.pid if c.proc else None) for c in self._children}

    # ---------------- 内部 ----------------

    def _spawn(self, child: _Child) -> None:
        # 上一次的日志句柄先关掉：崩溃重启会再次走 _spawn，不关会累积 Windows 文件句柄
        # （且旧句柄一直占着同名 .log）。Popen 失败分支同样要关，见下。
        if child.log is not None:
            with contextlib.suppress(Exception):
                child.log.close()
                child.log = None
        log = None
        if self.log_dir is not None:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                # 名字与主日志同一套规则：``<组件>-YYYY-MM-DD.log``（不是光秃秃的 worker.log）。
                from app.core.logging.setup.handlers import child_log_file  # 延迟导入避免环

                # **二进制追加**：子进程直接往这个 fd 写字节，父进程不参与编码转换。
                # 用文本模式（encoding="utf-8"）会让人误以为文件是 utf-8，实际子进程仍按
                # 自己的编码写 → 同一个文件里 UTF-8/GBK 混排，读出来就是乱码。
                log = child_log_file(child.spec.name, directory=self.log_dir).open("ab")
            except OSError:
                log = None
        child.log = log
        kwargs: dict[str, Any] = {
            "cwd": child.spec.cwd,
            # **强制子进程按 UTF-8 输出**：否则 Windows 下 Python 子进程按控制台代码页
            # （中文机常是 GBK）写日志，与 UTF-8 的主日志混在一起就成乱码。
            "env": {
                **os.environ,
                "PYTHONIOENCODING": "utf-8",  # Python 子进程的 stdout/stderr 编码
                "PYTHONUTF8": "1",  # 打开 UTF-8 模式（含文件系统/标准流）
                **(child.spec.env or {}),  # spec 可显式覆盖
            },
            "stdout": log or subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
        }
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            kwargs["creationflags"] = flags
        try:
            child.proc = subprocess.Popen(child.spec.argv, **kwargs)
        except OSError as exc:
            logger.error("拉起子进程失败（{}）：{}", child.spec.name, exc)
            child.proc = None
            # 新开的日志句柄在 Popen 抛错时没有进程在写它，关掉避免泄漏。
            if log is not None:
                with contextlib.suppress(Exception):
                    log.close()
                    child.log = None
            return
        child.last_start = time.monotonic()
        child.health_fails = 0  # 新进程从头计健康失败（去抖计数不跨进程累积）
        child.restarted_at = time.time()  # 本次拉起时刻（界面提示"刚自动重启过"用）
        if self._job is not None:
            _assign_to_job(self._job, child.proc)
        self._register(child)  # 登记 pid + 创建时刻（供下次启动清理残留）
        logger.info("子进程已拉起：{}（pid {}）", child.spec.name, child.proc.pid)

    def _register(self, child: _Child) -> None:
        """把刚拉起的子进程写进登记表（pid + 创建时刻，供崩溃后清理）。"""
        path = self._registry
        proc = child.proc
        if path is None or proc is None:
            return
        created = _pid_liveness(proc.pid)[1]  # 本进程自己的创建时刻
        entries = [e for e in _read_registry(path) if int(e["pid"]) != int(proc.pid)]
        entries.append({"name": child.spec.name, "pid": int(proc.pid), "created": created})
        _write_registry(path, entries)

    def _unregister(self, child: _Child) -> None:
        """子进程确认退出后，从登记表里摘掉（避免下次启动白跑一趟）。"""
        path = self._registry
        proc = child.proc
        if path is None or proc is None:
            return
        entries = [e for e in _read_registry(path) if int(e["pid"]) != int(proc.pid)]
        _write_registry(path, entries)

    def _backoff(self, restarts: int) -> float:
        return min(self.backoff_max, self.backoff_base * (2 ** max(0, restarts - 1)))


def _safe_health(check: Callable[[], bool]) -> bool:
    try:
        return bool(check())
    except Exception:
        return False


def _terminate(proc: subprocess.Popen, *, grace_s: float = 3.0) -> str:
    """请求进程退出；宽限后 kill；确认后再返回。

    返回：``stopped``（宽限内退出）或 ``killed``（宽限后强杀）。已死返回 ``stopped``。
    """
    if proc is None or proc.poll() is not None:
        return "stopped"
    try:
        proc.terminate()
    except OSError:
        proc.kill()  # Windows 上偶发 terminate 抛错：fallback 到强杀，别让进程永远杀不掉
        return "stopped" if _wait_exit(proc, 1.0) else "killed"
    if _wait_exit(proc, grace_s):
        return "stopped"
    with contextlib.suppress(OSError):
        proc.kill()
    return "stopped" if _wait_exit(proc, 1.0) else "killed"


def _wait_exit(proc: subprocess.Popen, timeout_s: float) -> bool:
    """等进程退出，超时返回 False。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


# ---------------- 残留子进程登记 / 清理 ----------------
#
# 为什么要有登记表：``stop()`` 只在"正常退出"时跑。主进程被任务管理器结束、被
# TerminateProcess、或 QtWebEngine 之类 C++ 层 FATAL 直接 abort 时，Python 侧的
# 清理逻辑（含 atexit）根本不会执行——Job Object 是内核兜底，但"分配进 Job 失败"
# 的环境下就会漏。把子进程 pid + 创建时刻落盘，下次启动先照单清理，才谈得上"保证"。
# 创建时刻用来防 PID 复用：同一个 pid 已被别的进程占用时绝不下手。


def child_registry_path(log_dir: Path | None) -> Path | None:
    """子进程登记文件路径（``data/children.json``）；没有 log_dir 就不记。"""
    return None if log_dir is None else log_dir.parent / CHILD_REGISTRY_NAME


def _read_registry(path: Path) -> list[dict[str, Any]]:
    """读登记表；文件不存在/损坏都返回空表（坏文件不能挡住启动）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    children = raw.get("children") if isinstance(raw, dict) else None
    if not isinstance(children, list):
        return []
    return [c for c in children if isinstance(c, dict) and isinstance(c.get("pid"), int)]


def _write_registry(path: Path, entries: list[dict[str, Any]]) -> None:
    """写登记表（临时文件 + 原子替换，避免留下半截 JSON）；失败只记 debug。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"children": entries}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # 原子替换（Path.replace 会覆盖目标）
    except OSError as exc:
        logger.debug("写子进程登记表失败（不影响运行）：{}", exc)


def _open_process(pid: int, access: int) -> int | None:
    """按 pid 打开进程句柄；打不开（已退出/无权限）返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(access, False, int(pid))
        return int(handle) if handle else None
    except Exception:
        return None


def _close_handle(handle: int) -> None:
    """关闭内核句柄（失败无所谓）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        pass


def _process_created_at(handle: int) -> float | None:
    """进程创建时刻（Unix 秒）；读不到返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        creation = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            wintypes.HANDLE(handle),
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        return ticks / 1e7 - 11644473600.0  # FILETIME(1601) → Unix 秒
    except Exception:
        return None


def _is_handle_running(handle: int) -> bool:
    """句柄是否仍指向"活着的进程"。

    **不能只看 OpenProcess 是否成功**：Windows 上进程已终止、但还有人握着它的句柄
    （比如我们自己的 ``Popen`` 还没 poll）时，OpenProcess 依然成功。用
    ``WaitForSingleObject(0)`` 问内核：超时(258)=还在跑，已 signal=已退出。
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        return int(kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 0)) == 258  # WAIT_TIMEOUT
    except Exception:
        return True  # 判断不了就保守当作活着（后面强杀一次，多杀一次无害）


def _pid_liveness(pid: int) -> tuple[bool, float | None]:
    """pid 是否还活着 + 创建时刻（Unix 秒）；已退出返回 ``(False, None)``。"""
    if sys.platform != "win32":
        try:
            os.kill(int(pid), 0)
            return True, None
        except OSError:
            return False, None
    handle = _open_process(int(pid), 0x1000 | 0x00100000)  # QUERY_LIMITED | SYNCHRONIZE
    if handle is None:
        return False, None
    try:
        alive = _is_handle_running(handle)
        return alive, (_process_created_at(handle) if alive else None)
    finally:
        _close_handle(handle)


def _terminate_pid(pid: int) -> bool:
    """直接 ``TerminateProcess`` 结束一个进程（不作用于子孙）；成功返回 True。"""
    handle = _open_process(int(pid), 0x0001)  # PROCESS_TERMINATE
    if handle is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        return bool(kernel32.TerminateProcess(wintypes.HANDLE(handle), 1))
    except Exception as exc:
        logger.debug("TerminateProcess 失败（pid {}）：{}", pid, exc)
        return False
    finally:
        _close_handle(handle)


def _kill_pid_tree(pid: int) -> None:
    """强杀整个进程树：优先 ``taskkill /T /F``，被拒/失败再直接 TerminateProcess。

    **两条路都要有**：受限环境里 ``taskkill`` 可能被拒绝（实测返回
    ``ERROR: Access denied``），而 ``TerminateProcess`` 对**自己拉起的子进程**是允许的；
    只走一条就会"报告杀不掉、其实能杀"（或者反过来留下孤儿）。
    """
    pid = int(pid)
    if sys.platform == "win32":
        try:
            result = subprocess.run(  # 先试整树（连子孙一起）
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return  # 整树已结束
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("taskkill 调用失败（pid {}）：{}", pid, exc)
        if _terminate_pid(pid):  # 退回直接结束本进程（子孙由后续确认兜着）
            return
    with contextlib.suppress(OSError):
        os.kill(pid, 9)


def _wait_pid_gone(pid: int, timeout_s: float) -> bool:
    """等某个 pid 消失，超时返回 False（按句柄判断，不依赖 parent/child 关系）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_liveness(pid)[0]:
            return True
        time.sleep(0.05)
    return not _pid_liveness(pid)[0]


def cleanup_stale_children(log_dir: Path | None) -> dict[str, list[str]]:
    """启动时清理上次退出残留的子进程；返回 ``{"killed": [...], "failed": [...]}``。

    - 只对**登记表里记过、且创建时刻对得上**的 pid 下手（防 PID 复用误杀）；
    - 已经自己退出的条目直接丢弃；
    - 杀不掉的留在登记表里，下次启动继续尝试，并如实报给上层去提示用户。
    """
    result: dict[str, list[str]] = {"killed": [], "failed": []}
    path = child_registry_path(log_dir)
    if path is None:
        return result
    entries = _read_registry(path)
    if not entries:
        with contextlib.suppress(OSError):
            path.unlink()  # 没有登记就删掉空文件
        return result
    remaining: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("name") or "?")
        pid = int(entry["pid"])
        created = entry.get("created")
        alive, actual = _pid_liveness(pid)
        if not alive:
            continue  # 上次已经自己退出了
        if (
            isinstance(created, (int, float))
            and actual is not None
            and abs(float(actual) - float(created)) > 2.0
        ):
            logger.warning("残留登记 pid {}（{}）已被别的进程复用，跳过", pid, name)
            continue  # 不碰别人的进程；条目也不再保留
        _kill_pid_tree(pid)
        if _wait_pid_gone(pid, STOP_FORCE_WAIT_S):
            logger.warning("已结束上次启动残留的子进程：{}（pid {}）", name, pid)
            result["killed"].append(f"{name}({pid})")
        else:
            logger.error("上次启动残留的子进程无法结束：{}（pid {}）", name, pid)
            result["failed"].append(f"{name}({pid})")
            remaining.append(entry)  # 留到下次启动再试
    if remaining:  # 还有没清掉的：保留登记，下次继续
        _write_registry(path, remaining)
    else:  # 全清干净了：不留空文件
        with contextlib.suppress(OSError):
            path.unlink()
    return result



def _create_job_object() -> int | None:
    """Windows Job Object（KILL_ON_JOB_CLOSE）：主进程死了，子进程一起死。

    为什么必须：``on_quit`` 只能覆盖正常关窗；进程被任务管理器/崩溃杀掉时，
    Qdrant 和 worker 会变成孤儿继续占端口。把子进程放进一个"关闭即杀"的 Job，
    内核会在主进程句柄关闭（进程退出）时连带结束它们。

    非 Windows/受限环境返回 None（不影响启动，只是没有这层兜底）。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXTENDED()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = 0x00002000
        ok = kernel32.SetInformationJobObject(
            wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)  # 9=Extended
        )
        if not ok:
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return None
        return int(job)
    except Exception as exc:  # ctypes/权限问题不该挡住服务启动
        logger.debug("创建 Job Object 失败（不影响启动）：{}", exc)
        return None


def _close_job(job: int) -> None:
    """关闭 Job 句柄（KILL_ON_JOB_CLOSE 会顺带结束残余子进程）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(job))
    except Exception as exc:
        logger.debug("关闭 Job Object 失败（不影响退出）：{}", exc)


def _assign_to_job(job: int, proc: subprocess.Popen) -> None:
    """把子进程放进 Job；失败只记 debug（受限环境可能不允许嵌套 Job）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        handle = int(proc._handle)  # subprocess 的进程句柄只有这个入口
        if not kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(handle)):
            logger.debug("把子进程放进 Job Object 失败（pid {}）：{}", proc.pid, ctypes.get_last_error())
    except Exception as exc:
        logger.debug("Job Object 分配失败（不影响运行）：{}", exc)
