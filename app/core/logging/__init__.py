"""日志分区：进程级日志系统。

对外只暴露 :func:`app.core.logging.setup.init_logging`（初始化）、
:func:`~app.core.logging.setup.install_exception_hooks`（未捕获异常也进日志）与
:func:`~app.core.logging.setup.redact`（脱敏，供别处复用）。
"""

from app.core.logging.setup import (
    LOG_FILENAME,
    init_logging,
    install_exception_hooks,
    is_initialized,
    log_file,
    redact,
)

__all__ = [
    "LOG_FILENAME",
    "init_logging",
    "install_exception_hooks",
    "is_initialized",
    "log_file",
    "redact",
]
