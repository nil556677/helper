"""子进程守护：拉起 / 健康检查 / 退避重启 / 优雅停止。

Qdrant sidecar 与独立 worker 都复用它。**主进程是唯一守护者**：
- 子进程崩溃 → 按指数退避重启；短时间内反复崩溃 → 放弃并标降级；
- 退出 → 先请求子进程 terminate，宽限后 kill；
- Windows 用 ``CREATE_NO_WINDOW`` 避免黑框。

Job Object（主进程被强杀时连带杀子进程）见 :func:`_create_job_object`。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


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
                if child.spec.health is None or _safe_health(child.spec.health):
                    states[child.spec.name] = "running"
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
        # 最后再给一小段确认窗口：KILL_ON_JOB_CLOSE 之后仍活着的才是真正的异常。
        for child in self._children:
            name = child.spec.name
            if report.get(name) == "not-started":
                continue
            proc = child.proc
            if proc is not None and not _wait_exit(proc, 0.5):
                logger.error("子进程在停止后仍未退出：{}（pid {}）", child.spec.name, proc.pid)
                report[name] = "still-running"
        return report

    def pids(self) -> dict[str, int | None]:
        return {c.spec.name: (c.proc.pid if c.proc else None) for c in self._children}

    # ---------------- 内部 ----------------

    def _spawn(self, child: _Child) -> None:
        log = None
        if self.log_dir is not None:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                log = (self.log_dir / f"{child.spec.name}.log").open("a", encoding="utf-8")
            except OSError:
                log = None
        child.log = log
        kwargs: dict[str, Any] = {
            "cwd": child.spec.cwd,
            "env": {**os.environ, **(child.spec.env or {})},
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
            return
        child.last_start = time.monotonic()
        if self._job is not None:
            _assign_to_job(self._job, child.proc)
        logger.info("子进程已拉起：{}（pid {}）", child.spec.name, child.proc.pid)

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
