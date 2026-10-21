"""环境快照 / 统计：收集"这次是在什么环境里跑的"，以及磁盘、目录大小的展示工具。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.paths import file_size

from .filters import RUN_ID
from .handlers import MIN_FREE_MB


def _distribution_version(name: str) -> str:
    """读某个发行版的版本号（**不导入它**：core 不许 import 界面库，QtWebEngine 也一样）。"""
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return "—"


def _has_console() -> str:
    """这次是不是从控制台启动的（双击启动没有控制台，是"看不到报错"的根源）。"""
    try:
        import ctypes

        return "有" if ctypes.windll.kernel32.GetConsoleWindow() else "无（双击启动的样子）"
    except Exception:
        return "未知"


def _dir_size(directory: Path) -> int:
    """目录下**一层**文件的总大小（不递归：``data/`` 可能有几十万向量，递归会拖慢启动）。"""
    try:
        return sum(file_size(item) for item in directory.iterdir() if item.is_file())
    except OSError:
        return 0


def _human_size(size: int) -> str:
    """人读的大小（KB / MB / GB）。"""
    for unit, scale in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def check_disk_space(path: Path, min_free_mb: int = MIN_FREE_MB) -> str | None:
    """看磁盘剩余空间够不够；返回告警文本（够用时 None）。

    为什么要有：日志写不进去时 loguru 只会把错误打到 stderr（``catch=True``），而在同一个满盘上
    连错误都写不下——于是"日志突然没了"完全无迹可寻。启动时先看一眼，能提前告诉用户。
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    if usage.free < min_free_mb * 1024 * 1024:
        return (
            f"磁盘剩余空间不足：{path} 所在盘只剩 {_human_size(usage.free)}"
            f"（低于 {min_free_mb} MB），日志与数据可能写不进去"
        )
    return None


def environment_snapshot(extra: dict[str, str] | None = None) -> dict[str, str]:
    """收集"这次是在什么环境里跑的"（技术方案 §10.3）。

    参数：
        extra: 入口补充的信息（例如屏幕尺寸/DPI —— 那要 Qt，而 core 不许 import 界面库）。
    返回：要写进日志的键值对（顺序稳定，方便人读）。
    """
    info: dict[str, str] = {
        "run": RUN_ID,
        "Python": sys.version.split()[0],
        "PySide6": _distribution_version("PySide6"),
        "QtWebEngine": _distribution_version("PySide6-WebEngine"),
        "NiceGUI": _distribution_version("nicegui"),
        "onnxruntime": _distribution_version("onnxruntime"),
        "控制台": _has_console(),
        "CWD": str(Path.cwd()),
        "TEMP": tempfile.gettempdir(),
        "数据目录": str(paths.data_dir()),
        "日志目录": str(paths.logs_dir()),
        "Chromium 参数": os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "—"),
        "日志目录占用": _human_size(_dir_size(paths.logs_dir())),
        "数据库大小": _human_size(file_size(paths.db_file())),
    }
    info.update(extra or {})
    return info


def log_environment_snapshot(extra: dict[str, str] | None = None) -> None:
    """把环境快照写进日志（一行一项，出问题时先看这一段）。"""
    info = environment_snapshot(extra)
    logger.info("环境快照：{}", "；".join(f"{key}={value}" for key, value in info.items()))
