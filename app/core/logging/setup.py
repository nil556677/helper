"""日志系统：进程级唯一初始化入口。

设计要点（每条都对应真实踩过的坑，改之前先看这里）：

1. **每个 sink 都要显式 ``diagnose=False``。** loguru 的默认值是 ``True``，异常栈会把
   **局部变量**一起打出来——而局部变量里就有 API key 明文，用户一截图就等于泄密。
   ``diagnose`` 只对**传了参数的那个 sink** 生效，所以每加一个 sink 都要补上。
2. **不假设"patcher 能脱敏"。** patcher 只看得到 ``record["message"]``，异常文本是在
   格式化阶段拼进 ``record["exception"]`` 的。挡泄漏真正靠的是第 1 条（不开 diagnose）；
   :func:`redact` 只兜住"用户自己把 key 拼进日志文本"这种情况，**它不是安全边界**。
3. **控制台只留 WARNING，完整信息进文件。** 桌面程序没有常驻控制台，INFO 只会淹没真正的报错。
4. **未捕获异常必须进日志**（:func:`install_exception_hooks`）：双击启动的窗口没有控制台，
   崩在界面回调或工作线程里时用户只看到"闪一下就没了"，没有钩子我们什么都拿不到。
5. **初始化幂等。** 每次调用先 ``logger.remove()`` 清掉旧 sink 再加，避免重复初始化后
   一行日志写两份。
6. **日志文件写不进去也不能让程序起不来**：退回只输出控制台，并给出明确告警。
7. **不用系统 TEMP**：临时产物统一放 ``{安装目录}/.tmp``（见 core/paths.tmp_dir）。
"""

from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

from loguru import logger

from app.core import paths

#: 日志文件名（位于 :func:`app.core.paths.logs_dir`）。
LOG_FILENAME = "helper.log"

#: 单文件上限与保留份数：本地小工具，别让日志把磁盘吃满。
ROTATION = "5 MB"
RETENTION = 5

#: 文件 sink 的格式。loguru 对 ``colorize=False`` 的 sink 会自动去掉颜色标签。
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}"

#: 形似密钥的片段：兜底脱敏用（见模块说明第 2 条）。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"((?i:api[_-]?key)\s*[=:]\s*)[A-Za-z0-9_\-]{12,}"), r"\1***"),
    (re.compile(r"((?i:authorization:\s*bearer\s+))[A-Za-z0-9._\-]{12,}"), r"\1***"),
)

#: 当前生效的日志文件（文件 sink 建不起来时为 None）。
_log_file: Path | None = None

_initialized = False


def redact(text: str) -> str:
    """把文本里形似密钥的片段打码。

    参数：
        text: 原始文本（日志消息或要展示的异常说明）。
    返回：打码后的文本；没有命中模式时原样返回。

    注意：这是**兜底**，不是安全边界。真正防泄漏的是"不给 loguru 开 diagnose"。
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _patch(record: object) -> None:
    """loguru patcher：对 message 做兜底脱敏。

    局部变量不经过这里（见模块说明第 2 条），所以别把安全责任放在这个函数上。
    """
    message = record["message"]  # type: ignore[index]
    if isinstance(message, str):
        record["message"] = redact(message)  # type: ignore[index]


def init_logging(
    level: str = "INFO",
    *,
    log_file: Path | None = None,
    console_level: str = "WARNING",
    rotation: str = ROTATION,
    retention: int = RETENTION,
) -> Path | None:
    """初始化进程日志（幂等：重复调用按新参数重建 sink）。

    参数：
        level: 文件 sink 的级别（``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``）。
            值来自引导配置 ``config.json`` 的 ``log_level``（技术方案 §12）。
        log_file: 日志文件路径；默认 ``{安装目录}/logs/helper.log``（测试可指定临时文件）。
        console_level: 控制台 sink 的级别，默认只放 WARNING 以上。
        rotation: 单文件上限（loguru 写法，如 ``"5 MB"``）。
        retention: 保留份数。
    返回：
        实际生效的日志文件路径；**文件写不进去时返回 None**（此时只有控制台 sink）。
    """
    global _log_file, _initialized

    # 先清掉旧 sink：不清的话重复初始化会让一行日志写两份。
    logger.remove()
    logger.configure(patcher=_patch)

    target = log_file or (paths.logs_dir() / LOG_FILENAME)
    _log_file = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(target),
            level=level.upper(),
            format=LOG_FORMAT,
            encoding="utf-8",
            rotation=rotation,
            retention=retention,
            enqueue=False,  # 受限令牌下建不了命名管道，异步队列会被拒；同步写足够快
            backtrace=False,
            diagnose=False,  # 见模块说明第 1 条：异常栈不许带局部变量
            catch=True,  # 日志自身出错不该把业务代码带崩
        )
        _log_file = target
    except OSError as exc:  # noqa: BLE001 - 允许失败：日志目录不可写也要能启动
        # 退回控制台：装到只读位置时至少还能看到告警，而不是直接起不来。
        logger.add(sys.stderr, level=console_level.upper(), backtrace=False, diagnose=False)
        logger.warning("日志文件不可用（{}），本次只输出到控制台：{}", target, exc)
        _initialized = True
        return None

    logger.add(
        sys.stderr,
        level=console_level.upper(),
        format=LOG_FORMAT,
        backtrace=False,
        diagnose=False,  # 第二个 sink 也必须显式关（默认是 True）
        catch=True,
    )
    _initialized = True
    logger.debug("日志已初始化：{}（级别 {}）", target, level.upper())
    return target


def install_exception_hooks() -> None:
    """把"没人接"的异常送进日志（主线程 + 子线程）。

    为什么必须有：桌面程序崩在界面回调或工作线程里时，Python 默认只往 stderr 打一行；
    双击启动的窗口没有控制台，用户看到的是"窗口闪一下就没了"，而我们拿不到任何现场。

    幂等：重复调用只是把钩子重新装一遍。
    """

    def _main_hook(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        # Ctrl+C 是用户意图，不是故障，不该记成 CRITICAL。
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)  # type: ignore[arg-type]
            return
        logger.opt(exception=(exc_type, exc, tb)).critical("未捕获异常，程序可能已不可用")

    def _thread_hook(args: object) -> None:
        if issubclass(args.exc_type, SystemExit):  # type: ignore[attr-defined]
            return
        thread_name = getattr(getattr(args, "thread", None), "name", "?")
        logger.opt(
            exception=(
                args.exc_type,  # type: ignore[attr-defined]
                args.exc_value,  # type: ignore[attr-defined]
                args.exc_traceback,  # type: ignore[attr-defined]
            )
        ).critical("子线程内未捕获异常（线程 {}）", thread_name)

    sys.excepthook = _main_hook
    threading.excepthook = _thread_hook


def is_initialized() -> bool:
    """日志系统是否已初始化（启动自检用）。"""
    return _initialized


def log_file() -> Path | None:
    """当前日志文件路径；只有控制台 sink 时返回 None。"""
    return _log_file
