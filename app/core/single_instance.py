"""单实例守卫（Windows 命名互斥体）。

**为什么需要**：两个实例同时写同一个 SQLite / 抢 Qdrant 的单进程锁会互相打。今天实测出现过
10 个残留实例（多次重启没杀干净）。

**用 ``CreateMutexW``**：简单、进程结束自动释放（不用怕"崩溃导致锁没释放"），
而且名字按安装目录派生——两个不同 checkout 可以各跑一个，同一目录只能一个。

**两条铁律（都会影响"会不会挡住用户"）**：

1. **守卫只在 ``app/__main__.py`` 的 ``main()`` 里安装**，测试与脚本（``--selfcheck``、
   ``check_ui.py``）都不走 ``main()``，所以不会被守卫影响——**别在 bootstrap 里加守卫**，
   否则会把测试进程互相挡死。
2. **守卫坏掉不许挡住启动**：API 不可用时返回 ``True``（放行）并告警，而不是崩掉。
"""

from __future__ import annotations

import contextlib
import ctypes
from pathlib import Path

from loguru import logger

#: 互斥体名字前缀（后缀是安装目录的 sha1 前 12 位）。
MUTEX_PREFIX = "helper-single-instance-"

#: Windows 的错误码：名字已存在 → 已有另一个实例。
ERROR_ALREADY_EXISTS = 183

#: 已取得的互斥体句柄（模块级：活着到进程结束，取到就别反复开）。
_mutex_handle: object | None = None

#: kernel32 的句柄，**必须 use_last_error=True**。默认的 ``ctypes.windll`` 是
#: ``use_last_error=False``，调用后 ``ctypes.get_last_error()`` 恒返回 0，判断不了
#: ``ERROR_ALREADY_EXISTS``——那会让单实例守卫**完全失效**（第二个实例也放行）。
#: 实测验证过两个分支（见 tests/unit/test_single_instance.py）。
_kernel32 = None


def _kernel() -> object:
    """取 use_last_error=True 的 kernel32（首次调用时惰性建，留模块级变量复用）。"""
    global _kernel32
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return _kernel32


def instance_key(root: Path) -> str:
    """按安装目录派生互斥体名字：两个不同 checkout 各跑一个，同一目录只能一个。

    用 ``sha1(str(root).lower())`` —— 大小写不同但指向同一个目录时不该放行两个。
    """
    import hashlib

    digest = hashlib.sha1(str(root).lower().encode("utf-8")).hexdigest()[:12]
    return MUTEX_PREFIX + digest


def acquire(root: Path | None = None) -> bool:
    """取命名互斥体。

    返回：
        ``True`` = 拿到了（本进程是唯一实例，或守卫不可用被跳过）；
        ``False`` = 已有另一个实例在跑。

    守卫不可用（API 失败）时**返回 True 并告警**——守卫坏掉不该挡住启动。
    句柄存模块级变量，活到进程结束（进程退出时 OS 自动释放）。
    """
    global _mutex_handle
    if _mutex_handle is not None:
        return True  # 本进程已经取过

    from app.core import paths

    target = root if root is not None else paths.project_root()
    name = instance_key(target)
    try:
        kernel = _kernel()
        handle = kernel.CreateMutexW(None, False, name)
        last_error = ctypes.get_last_error()
    except (AttributeError, OSError) as exc:
        logger.warning("单实例守卫不可用（{}），跳过守卫：{}", type(exc).__name__, exc)
        return True
    if not handle:
        logger.warning("CreateMutexW 失败（错误 {}），跳过守卫", last_error)
        return True

    if last_error == ERROR_ALREADY_EXISTS:
        # 名字已存在：已经有别的实例拿着这个互斥体。CreateMutexW 在已存在时仍返回
        # **有效句柄**，这里必须关掉再返回 False，否则句柄泄漏。
        with contextlib.suppress(AttributeError, OSError):
            kernel.CloseHandle(handle)
        logger.info("检测到已有另一个实例（互斥体 {} 已被占用）", name)
        return False

    _mutex_handle = handle
    logger.debug("单实例守卫已获取：{}", name)
    return True


def release() -> None:
    """释放互斥体句柄（进程退出一般不主动调；OS 会自动放。测试里用）。"""
    global _mutex_handle
    handle = _mutex_handle
    _mutex_handle = None
    if handle:
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.kernel32.CloseHandle(handle)


def find_window(title: str = "helper") -> int:
    """按窗口标题找句柄（尽力而为；找不到返回 0）。"""
    try:
        return int(ctypes.windll.user32.FindWindowW(None, title))
    except (AttributeError, OSError):
        return 0


def activate_existing_window(title: str = "helper", *, wait_s: float = 2.5) -> bool:
    """把已存在的窗口切到前台（找不到就返回 False，不报错）。

    **要等一小会儿**：第二个实例拿到"已有实例"的结论时，第一个实例可能还在
    ``ensure_ready()`` 里、窗口尚未创建——此时 ``FindWindowW`` 找不到，直接返回 False
    的话用户只会看到一句"已经在运行了"，窗口却没到前面（实测过）。这里在 ``wait_s``
    内轮询，等到就切。

    只尽力而为：`SetForegroundWindow` 有系统限制（可能只闪一下任务栏）。
    """
    import time

    deadline = time.monotonic() + max(0.0, float(wait_s))
    hwnd = 0
    while time.monotonic() < deadline:
        hwnd = find_window(title)
        if hwnd:
            break
        time.sleep(0.15)
    if not hwnd:
        return False
    try:
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return True
    except (AttributeError, OSError):
        return False
