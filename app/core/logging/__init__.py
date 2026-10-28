"""日志分区：进程级日志系统（只干一件事——把全项目的错误记下来，见技术方案 §10）。

对外暴露的东西分三类：

- **初始化**：:func:`~app.core.logging.setup.init_logging`；
- **装钩子**（入口专用，都是进程级副作用）：stdlib 桥、asyncio 策略、原生输出捕获、异常钩子；
- **读状态 / 工具**：:func:`~app.core.logging.setup.log_file`、``redact``、环境快照、过期清理、受限嗅探。
"""

from app.core.logging.setup import (
    CONSOLE_PREFIX,
    LOG_FILENAME,
    LOG_FILENAME_PREFIX,
    LOGS_TOTAL_LIMIT_MB,
    RETENTION_DAYS,
    ROTATION_SIZE_MB,
    RUN_ID,
    capture_native_output,
    check_disk_space,
    child_log_file,
    console_file,
    console_output_file,
    daily_log_file,
    environment_snapshot,
    init_child_logging,
    init_logging,
    install_asyncio_handler,
    install_exception_hooks,
    install_stdlib_bridge,
    is_initialized,
    log_environment_snapshot,
    log_file,
    new_run_id,
    purge_old_logs,
    redact,
    sniff_restricted_hint,
)

__all__ = [
    "CONSOLE_PREFIX",
    "LOGS_TOTAL_LIMIT_MB",
    "LOG_FILENAME",
    "LOG_FILENAME_PREFIX",
    "RETENTION_DAYS",
    "ROTATION_SIZE_MB",
    "RUN_ID",
    "capture_native_output",
    "check_disk_space",
    "child_log_file",
    "console_file",
    "console_output_file",
    "daily_log_file",
    "environment_snapshot",
    "init_child_logging",
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
