"""旧日志清理 / 受限环境嗅探。

过期与超量清理（:func:`purge_old_logs`）和"上次是不是被沙箱挡住"的判断（:func:`sniff_restricted_hint`）
放在一起：它们都只扫 ``logs/`` 目录、都不持有进程级可变状态，是纯工具。
"""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from app.core import paths

from .handlers import (
    CONSOLE_PREFIX,
    LOG_FILENAME,
    LOGS_TOTAL_LIMIT_MB,
    RETENTION_DAYS,
    console_output_file,
    log_file,
)

#: 受限（沙箱）环境下 Chromium 会打出的特征行：白板时的唯一现场。
_RESTRICTED_MARKERS: tuple[str, ...] = (
    "Check failed",
    "拒绝访问",
    "Failed to create GLES3 context",
    "Failed to create shared context",
    "Access is denied",
)


def purge_old_logs(days: int = RETENTION_DAYS, total_limit_mb: int = LOGS_TOTAL_LIMIT_MB) -> int:
    """清理 ``logs/``：先按保留期、再按总量上限，返回删了几个文件。

    为什么必须自己扫：``console-*.log`` 是 fd 直写的，loguru 根本不认识它们；主日志的轮转文件
    虽然由 loguru 的 ``retention`` 管，但"目录里别的东西"与**总量**归我们管。

    两道闸门：
        1. **保留期**（默认 7 天）：判据用**文件修改时间**（名字里的日期不可靠：用户可能改系统时间）；
        2. **总量上限**（默认 200 MB）：7 天里也可能堆出几个 G，超过就从最旧的开始删。

    两个**正在写的文件**（主日志 + 当天原生输出）永远不动——删掉它们只会让日志立刻开始报错。
    """
    protected = {path for path in (log_file(), console_output_file()) if path is not None}

    def _candidates() -> list[Path]:
        try:
            return [path for path in paths.logs_dir().glob("*.log") if path not in protected]
        except OSError:
            return []

    removed = 0

    # 闸门 1：保留期
    cutoff = time.time() - days * 86400
    for path in _candidates():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue

    # 闸门 2：总量（从最旧的开始删）
    try:
        files = sorted(_candidates(), key=lambda item: item.stat().st_mtime)
        total = sum(item.stat().st_size for item in files)
    except OSError:
        files, total = [], 0
    limit = total_limit_mb * 1024 * 1024
    while files and total > limit:
        oldest = files.pop(0)
        try:
            size = oldest.stat().st_size
            oldest.unlink()
            total -= size
            removed += 1
        except OSError:
            continue

    if removed:
        logger.info("清理日志 {} 个（保留最近 {} 天，总量上限 {} MB）", removed, days, total_limit_mb)
    return removed


def _read_tail(path: Path, limit: int = 60_000) -> str:
    """读文件末尾若干字节（日志可能很大，不整份读进来）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def sniff_restricted_hint() -> str | None:
    """从原生输出与主日志里判断"上次启动是不是被沙箱挡住了"，返回一句人话（否则 None）。

    判据（技术方案 §10.3）：``console-*.log`` 里出现受限环境的特征行，**并且**主日志里
    最后一次"界面服务就绪"之后没有"界面已渲染" —— 那就是窗口没渲染出来。
    这句话直接写进日志与启动失败记录，省掉一轮"白板到底为什么"的排查。
    """
    markers: set[str] = set()
    for path in sorted(paths.logs_dir().glob(f"{CONSOLE_PREFIX}*.log"))[-2:]:
        text = _read_tail(path)
        markers.update(marker for marker in _RESTRICTED_MARKERS if marker in text)
    if not markers:
        return None

    main = _read_tail(paths.logs_dir() / LOG_FILENAME, limit=200_000)
    ready = main.rfind("界面服务就绪")
    rendered = main.rfind("界面已渲染")
    if ready >= 0 and rendered < ready:
        return (
            "上次启动疑似受限环境：原生输出里有 " + "、".join(sorted(markers)) +
            "，而且「界面服务就绪」之后没有出现「界面已渲染」——窗口大概率没渲染出来。"
        )
    return "原生输出里有受限环境特征（" + "、".join(sorted(markers)) + "），但上次窗口是渲染成功的。"
