"""日志系统：进程级唯一初始化入口。

**日志只干一件事：把整个项目的错误都记下来，让用户能自己排查问题**（技术方案 §10）。
不扩展功能、不暴露旋钮。

## 四个通道

1. **主日志**（本模块的文件 sink）：我们自己的记录 + 从 stdlib ``logging`` 收编来的第三方记录；
2. **第三方日志**：:func:`install_stdlib_bridge` 劫持 stdlib —— NiceGUI / uvicorn / asyncio / PySide6 全走它，
   不接就等于把界面层的错误全丢掉；
3. **原生输出**：:func:`capture_native_output` 把 fd 1+2 重定向到 ``logs/console-<日期>.log`` ——
   Qt / Chromium 的 C 层输出只能从这里拿到（**白板那次的现场全在这一类里**）；
4. **启动失败**：入口把现场写进 ``logs/startup-error.log`` 并弹提示框（见 ``app/__main__.py``）。

## 为什么这里要这么绕（每条都对应真实踩过的坑）

1. **每个 sink 都要显式 ``diagnose=False``。** loguru 默认是 ``True``，异常栈会把**局部变量**
   一起打出来——而局部变量里就有 API key 明文，用户一截图就等于泄密。
2. **不假设"patcher 能脱敏"。** patcher 只看得到 ``record["message"]``；异常文本是在格式化阶段
   拼进 ``record["exception"]`` 的。挡泄漏真正靠的是第 1 条，:func:`redact` 只兜
   "用户自己把 key 拼进日志文本"这种情况，**它不是安全边界**。
3. **原生输出无法在流里脱敏**（fd 重定向是字节流）→ 所以我们自己绝不往 stdout/stderr 打敏感内容。
4. **接 asyncio 异常处理器**（:func:`install_asyncio_handler`）：NiceGUI 的事件回调跑在任务里，
   任务异常默认只打到 stderr —— 表现就是"点了没反应，日志里什么都没有"，这是最难查的一类问题。
5. **控制台只留 WARNING，完整信息进文件。** 桌面程序没有常驻控制台，INFO 只会淹没真正的报错。
6. **未捕获异常必须进日志**（:func:`install_exception_hooks`）：双击启动的窗口没有控制台，
   崩在界面回调或工作线程里时用户只看到"闪一下就没了"，没有钩子我们什么都拿不到。
7. **初始化幂等。** 每次调用先 ``logger.remove()`` 清掉旧 sink 再加，避免重复初始化后一行写两份。
8. **日志文件写不进去也不能让程序起不来**：退回只输出控制台，并给出明确告警。
9. **不用系统 TEMP**：临时产物统一放 ``{安装目录}/.tmp``（见 core/paths.tmp_dir）。
10. **按天轮转 + 只留 7 天**：:data:`RETENTION_DAYS`。单文件超过 :data:`ROTATION_SIZE_MB` 也轮转
    （先到先得）——防止某天一个循环刷满磁盘。``console-*.log`` 是 fd 直写、loguru 管不到，
    过期清理由 :func:`purge_old_logs` 自己扫。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from app.core import paths

#: 日志文件名（位于 :func:`app.core.paths.logs_dir`）。
LOG_FILENAME = "helper.log"

#: 原生输出（fd 1+2）的当天文件名前缀，实际是 ``console-YYYY-MM-DD.log``。
CONSOLE_PREFIX = "console-"

#: 保留天数（技术方案 §10.2：只留最近 7 天）。
RETENTION_DAYS = 7

#: 单文件上限（MB）：同一天里超过就轮转，防止某个死循环把磁盘刷满。
ROTATION_SIZE_MB = 20

#: 文件 sink 的格式。loguru 对 ``colorize=False`` 的 sink 会自动去掉颜色标签。
#: ``{extra[run]}`` 是本次运行的 run id（由 :func:`new_run_id` 生成，patcher 保证一定有值）。
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[run]} | {name}:{line} | {message}"

#: 本次运行的短 id：把"一次启动"的所有记录串起来（用户报"刚才那次"，我们能一眼筛出来）。
RUN_ID = uuid.uuid4().hex[:6]

#: 形似密钥的片段：兜底脱敏用（见模块说明第 2 条）。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"((?i:api[_-]?key)\s*[=:]\s*)[A-Za-z0-9_\-]{12,}"), r"\1***"),
    (re.compile(r"((?i:authorization:\s*bearer\s+))[A-Za-z0-9._\-]{12,}"), r"\1***"),
)

#: 受限（沙箱）环境下 Chromium 会打出的特征行：白板时的唯一现场。
_RESTRICTED_MARKERS: tuple[str, ...] = (
    "Check failed",
    "拒绝访问",
    "Failed to create GLES3 context",
    "Failed to create shared context",
    "Access is denied",
)

#: 特别吵的第三方 logger：它们的 INFO 里没有我们需要的诊断信息，抬到 WARNING。
#: 实测 python-socketio 会把每个网络包按 INFO 打一行（含整份界面载荷，30 秒 1.5 MB）。
_QUIET_LEVELS: dict[str, int] = {
    "socketio": logging.WARNING,
    "engineio": logging.WARNING,
    "uvicorn.access": logging.WARNING,
    "watchfiles": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "websockets": logging.WARNING,
}

#: 当前生效的日志文件（文件 sink 建不起来时为 None）。
_log_file: Path | None = None

#: 原生输出的当天文件（没捕获时为 None）。
_console_file: Path | None = None

_initialized = False

#: 这些东西只该做一次（``init_logging`` 会被重复调用，见模块说明第 7 条）。
_bridge_installed = False
_asyncio_installed = False
_fd_captured = False


def new_run_id() -> str:
    """给本次运行换一个新的 run id（测试用；正常运行只在进程启动时定一次）。"""
    global RUN_ID
    RUN_ID = uuid.uuid4().hex[:6]
    return RUN_ID


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
    """loguru patcher：兜底脱敏 + 保证 run id 一定存在。

    局部变量不经过这里（见模块说明第 2 条），所以别把安全责任放在这个函数上。
    """
    message = record["message"]  # type: ignore[index]
    if isinstance(message, str):
        record["message"] = redact(message)  # type: ignore[index]
    record["extra"].setdefault("run", RUN_ID)  # type: ignore[index]


def _needs_rotation(message: str, file: object) -> bool:
    """轮转判据：跨天（``00:00`` 之后第一条）或单文件超过上限。

    loguru 的 ``rotation`` 只接受"按时间"或"按大小"两者之一，这里用一个可调用对象把两条
    合成"先到先得"：桌面程序既不能一天一个文件涨到几个 G，也不能文件多到翻不过来。
    """
    path = Path(str(file.name))
    try:
        stat = path.stat()
    except OSError:
        return False
    if time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime)) != time.strftime("%Y-%m-%d"):
        return True
    return stat.st_size >= ROTATION_SIZE_MB * 1024 * 1024


def console_file(day: str | None = None) -> Path:
    """当天的原生输出文件路径（``logs/console-YYYY-MM-DD.log``）。"""
    stamp = day or time.strftime("%Y-%m-%d")
    return paths.logs_dir() / f"{CONSOLE_PREFIX}{stamp}.log"


def init_logging(
    level: str = "INFO",
    *,
    log_file: Path | None = None,
    console_level: str = "WARNING",
    rotation: str | Callable[[str, object], bool] | None = None,
    retention: str | int = f"{RETENTION_DAYS} days",
) -> Path | None:
    """初始化进程日志（幂等：重复调用按新参数重建 sink）。

    参数：
        level: 文件 sink 的级别（``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``）。
            值来自引导配置 ``config.json`` 的 ``log_level``（技术方案 §12）。
        log_file: 日志文件路径；默认 ``{安装目录}/logs/helper.log``（测试可指定临时文件）。
        console_level: 控制台 sink 的级别，默认只放 WARNING 以上。
        rotation: 轮转判据；默认"跨天或超过 20 MB"（见 :func:`_needs_rotation`）。
        retention: 保留策略，默认 7 天。
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
            rotation=rotation or _needs_rotation,
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


class InterceptHandler(logging.Handler):
    """把 stdlib ``logging`` 的记录转发给 loguru。

    为什么必须有（实测）：``logs/helper.log`` 里只有 loguru 自己的记录，而
    **NiceGUI / uvicorn / asyncio / PySide6 全部走 stdlib ``logging``** —— 不接这一层，
    界面层的错误就只打到 stderr，双击启动时等于没有。
    """

    def emit(self, record: logging.LogRecord) -> None:
        # 防回环：loguru 自己的记录不许再绕回来（否则 handler 会自己喂自己）。
        if record.name.startswith("loguru"):
            return
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # 找到真正的调用点：不跳过 logging 自身的栈帧，每行都会指向 logging/__init__.py
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def install_stdlib_bridge(level: int = logging.WARNING) -> InterceptHandler:
    """把 stdlib ``logging`` 全面导向 loguru（root + 已经存在的各库 logger）。

    **默认只要 WARNING 以上**：第三方库的 INFO 会把日志淹掉——实测 python-socketio 把**每一个
    网络包**都按 INFO 打一行（还带整份界面载荷），30 秒刷出 1.5 MB，既吵又是隐私风险。
    我们自己的记录走 loguru，不经过这里，所以它们该 INFO 还是 INFO。

    幂等：重复调用只是把 handler 换一遍（uvicorn 起服务时会重设自己的 logging 配置，
    所以入口在界面服务起来之后还要再调一次，见 ``app/__main__.py``）。
    """
    global _bridge_installed

    handler = InterceptHandler()
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(min(level, logging.WARNING))

    for name in list(logging.root.manager.loggerDict):
        candidate = logging.getLogger(name)
        if isinstance(candidate, logging.PlaceHolder):
            continue
        candidate.handlers = [handler]
        candidate.propagate = False
        # 特别吵的几个再抬一档：它们的 INFO 里没有我们需要的诊断信息
        candidate.setLevel(_QUIET_LEVELS.get(name, level))

    # 兜底：将来才创建的 logger 也要走这个 handler（root 上已经挂了，这里只管级别）
    for name, quiet in _QUIET_LEVELS.items():
        logging.getLogger(name).setLevel(quiet)

    _bridge_installed = True
    return handler


def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """事件循环里的未处理异常 → 日志（含"Task exception was never retrieved"这一类）。"""
    exception = context.get("exception")
    message = str(context.get("message") or "asyncio 未处理异常")
    if isinstance(exception, BaseException):
        logger.opt(exception=exception).error("事件循环里未捕获异常：{}", message)
    else:
        detail = context.get("future") or context.get("handle") or ""
        logger.error("事件循环里的未处理事件：{}（{}）", message, detail)


class _LoggingEventLoopPolicy(asyncio.DefaultEventLoopPolicy):  # type: ignore[misc]
    """每个新建的事件循环都装上异常处理器。

    为什么用"策略"而不是直接给某个 loop 装：NiceGUI/uvicorn 的事件循环是在**它自己的线程里**
    创建的，我们在主线程拿不到那个对象；换成策略就等于把钩子前置到"所有循环诞生"这一刻。
    """

    def new_event_loop(self) -> asyncio.AbstractEventLoop:
        loop = super().new_event_loop()
        loop.set_exception_handler(_asyncio_exception_handler)
        return loop


def install_asyncio_handler() -> None:
    """装事件循环策略（幂等）。见 :class:`_LoggingEventLoopPolicy`。"""
    global _asyncio_installed
    if _asyncio_installed:
        return
    asyncio.set_event_loop_policy(_LoggingEventLoopPolicy())
    _asyncio_installed = True


def capture_native_output(target: Path | None = None) -> Path | None:
    """把 fd 1 与 fd 2 重定向到当天的 ``console-<日期>.log``（只做一次）。

    为什么必须做：Qt / Chromium 的 C 层输出（``FATAL``、``GLES3`` 报错、崩溃前最后几行）
    **不经过 Python**，只能从进程的 stdout/stderr 文件描述符上拿。白板那次的全部现场
    都在这一类里，而双击启动没有常驻控制台 —— 于是日志里一行都没有，问题自然"排查不好"。

    ⚠ 重定向之后**终端不再显示**这些输出（它们进了文件）。这是刻意的取舍：桌面程序的正经现场
    是文件；需要边看边跑时，用 ``Get-Content logs/console-*.log -Wait`` 跟即可。
    不加"同时回显"的管道泵：那需要多一个线程，终端侧一旦卡住就会把整个进程堵死。

    返回：实际使用的文件路径；失败时返回 None（并告警，不影响启动）。
    """
    global _console_file, _fd_captured
    if _fd_captured:
        return _console_file

    path = target or console_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "ab", buffering=0)  # noqa: SIM115 - 这个句柄要和进程同寿
        os.dup2(handle.fileno(), 1)
        os.dup2(handle.fileno(), 2)
        # 双击启动（没有控制台）时 sys.stdout/stderr 可能指向**无效句柄**：那时任何 print 都会抛
        # `OSError: 句柄无效` —— 实测它会让 NiceGUI 的服务线程在打就绪横幅时直接死掉，表现是
        # "界面服务 20 秒没起来 + 窗口白板"。所以这里**总是**把两个流重建到刚接好的 fd 上
        # （UTF-8 + 行缓冲），保证进程里再没有任何一个流指着坏句柄。
        sys.stdout = os.fdopen(1, "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = os.fdopen(2, "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError as exc:  # noqa: BLE001 - 允许失败：拿不到原生输出也要能启动
        logger.warning("原生输出重定向失败（{}）：{}", path, exc)
        return None

    _console_file = path
    _fd_captured = True
    # 表头用**我们自己那个句柄**写，不用 print：双击启动（无控制台）时 sys.stdout 可能不可用，
    # 而 print 会因此抛 `OSError: 句柄无效` —— 实测就是它在双击场景里把启动打断的。
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        handle.write(f"\n===== 本次运行开始 {stamp} | run {RUN_ID} =====\n".encode())
    except OSError:
        pass
    return path


def console_output_file() -> Path | None:
    """当前原生输出文件；没捕获时为 None。"""
    return _console_file


def purge_old_logs(days: int = RETENTION_DAYS) -> int:
    """删掉 ``logs/`` 下超过保留期的日志文件，返回删了几个。

    为什么必须自己扫：``console-*.log`` 是 fd 直写的，loguru 根本不认识它们；
    主日志的轮转文件由 loguru 的 ``retention`` 管，但"目录里别的东西"归我们管。
    判据用**文件修改时间**（名字里的日期不可靠：用户可能改系统时间）。
    """
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        candidates = list(paths.logs_dir().glob("*.log"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("清理过期日志 {} 个（保留最近 {} 天）", removed, days)
    return removed


def _distribution_version(name: str) -> str:
    """读某个发行版的版本号（**不导入它**：core 不许 import 界面库，QtWebEngine 也一样）。"""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version(name)
    except Exception:  # noqa: BLE001 - 版本号缺失不该影响启动
        return "—"


def _has_console() -> str:
    """这次是不是从控制台启动的（双击启动没有控制台，是"看不到报错"的根源）。"""
    try:
        import ctypes

        return "有" if ctypes.windll.kernel32.GetConsoleWindow() else "无（双击启动的样子）"
    except Exception:  # noqa: BLE001
        return "未知"


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
    }
    info.update(extra or {})
    return info


def log_environment_snapshot(extra: dict[str, str] | None = None) -> None:
    """把环境快照写进日志（一行一项，出问题时先看这一段）。"""
    info = environment_snapshot(extra)
    logger.info("环境快照：{}", "；".join(f"{key}={value}" for key, value in info.items()))


def is_initialized() -> bool:
    """日志系统是否已初始化（启动自检用）。"""
    return _initialized


def log_file() -> Path | None:
    """当前日志文件路径；只有控制台 sink 时返回 None。"""
    return _log_file


def _read_tail(path: Path, limit: int = 60_000) -> str:
    """读文件末尾若干字节（日志可能很大，不整份读进来）。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
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
