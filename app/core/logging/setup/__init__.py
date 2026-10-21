"""日志系统：进程级唯一初始化入口（包）。

从单模块 ``setup.py`` 重构为子包 :mod:`app.core.logging.setup`，对外接口不变：
``from app.core.logging.setup import init_logging, ...`` 依旧可用。

职责拆分（每个子模块都在自己的文件里，函数体未改动）：
- :mod:`~app.core.logging.setup.handlers`：各种 handler 的工厂、按天文件 sink、初始化与各类钩子坝；
- :mod:`~app.core.logging.setup.filters`：loguru patcher 与兜底脱敏（:data:`RUN_ID` 在这里定值/换新）；
- :mod:`~app.core.logging.setup.overview`：环境快照 / 统计 / 磁盘余量检查；
- :mod:`~app.core.logging.setup.cleanup`：旧日志清理与受限环境嗅探。

这里**逐条再导出**上面所有对外名字（不用 ``import *``），保持旧模块的同名可见性。
"""

from .cleanup import (
    purge_old_logs,
    sniff_restricted_hint,
)
from .filters import (
    RUN_ID,
    new_run_id,
    redact,
)
from .handlers import (
    CONSOLE_PREFIX,
    LOG_FILENAME,
    LOG_FILENAME_PREFIX,
    LOG_FORMAT,
    LOGS_TOTAL_LIMIT_MB,
    MIN_FREE_MB,
    RETENTION_DAYS,
    ROTATION_SIZE_MB,
    DailyFileSink,
    InterceptHandler,
    capture_native_output,
    console_file,
    console_output_file,
    daily_log_file,
    init_logging,
    install_asyncio_handler,
    install_exception_hooks,
    install_stdlib_bridge,
    is_initialized,
    log_file,
)
from .overview import (
    check_disk_space,
    environment_snapshot,
    log_environment_snapshot,
)

__all__ = [
    "CONSOLE_PREFIX",
    "LOGS_TOTAL_LIMIT_MB",
    "LOG_FILENAME",
    "LOG_FILENAME_PREFIX",
    "LOG_FORMAT",
    "MIN_FREE_MB",
    "RETENTION_DAYS",
    "ROTATION_SIZE_MB",
    "RUN_ID",
    "DailyFileSink",
    "InterceptHandler",
    "capture_native_output",
    "check_disk_space",
    "console_file",
    "console_output_file",
    "daily_log_file",
    "environment_snapshot",
    "init_logging",
    "install_asyncio_handler",
    "install_exception_hooks",
    "install_stdlib_bridge",
    "is_initialized",
    "log_environment_snapshot",
    "log_file",
    "new_run_id",
    "purge_old_logs",
    "redact",
    "sniff_restricted_hint",
]
