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
import contextlib
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from app.core import paths

from .filters import RUN_ID, _patch

#: 主日志的文件名前缀：实际是 ``helper-YYYY-MM-DD.log``（与 ``console-YYYY-MM-DD.log`` 同一套规则）。
LOG_FILENAME_PREFIX = "helper-"

#: 兼容旧名：只用于"当前日志在哪"的描述与清理保护，不是实际写入路径。
LOG_FILENAME = "helper.log"

#: 原生输出（fd 1+2）的当天文件名前缀，实际是 ``console-YYYY-MM-DD.log``。
CONSOLE_PREFIX = "console-"

#: 启动时要求的最小剩余磁盘空间（MB）：低于它就在日志里告警。
MIN_FREE_MB = 200

#: 保留天数（技术方案 §10.2：只留最近 7 天）。
RETENTION_DAYS = 7

#: 单文件上限（MB）：同一天里超过就轮转，防止某个死循环把磁盘刷满。
ROTATION_SIZE_MB = 20

#: ``logs/`` 的**总量上限**（MB）：只保留 7 天不足以防"一天里堆出几个 G"
#: （错误风暴、死循环打日志），所以再加一道总量闸门：超过就从最旧的开始删。
#: **当前正在写的两个文件（主日志与当天原生输出）永不动。**
LOGS_TOTAL_LIMIT_MB = 200

#: 文件 sink 的格式。loguru 对 ``colorize=False`` 的 sink 会自动去掉颜色标签。
#: ``{extra[run]}`` 是本次运行的 run id（由 :func:`new_run_id` 生成，patcher 保证一定有值）。
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[run]} | {extra[src]}:{extra[at]} | {message}"

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

#: 替身流的引用（**必须留住**：否则它们被回收时会 close 掉自己的 fd）。
_stdout_keepalive: object | None = None
_stderr_keepalive: object | None = None

#: 这些东西只该做一次（``init_logging`` 会被重复调用，见模块说明第 7 条）。
_bridge_installed = False
_asyncio_installed = False
_fd_captured = False


def _needs_rotation(message: str, file: object) -> bool:
    """轮转判据：跨天（``00:00`` 之后第一条）或单文件超过上限。

    loguru 的 ``rotation`` 只接受"按时间"或"按大小"两者之一，这里用一个可调用对象把两条
    合成"先到先得"：桌面程序既不能一天一个文件涨到几个 G，也不能文件多到翻不过来。
    """
    _ = message  # 名字是 loguru 回调要求的签名；轮转判据只用文件大小与时间
    path = Path(str(file.name))
    try:
        stat = path.stat()
    except OSError:
        return False
    if time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime)) != time.strftime("%Y-%m-%d"):
        return True
    return stat.st_size >= ROTATION_SIZE_MB * 1024 * 1024


def daily_log_file(day: str | None = None) -> Path:
    """当天的主日志文件路径（``logs/helper-YYYY-MM-DD.log``）。"""
    stamp = day or time.strftime("%Y-%m-%d")
    return paths.logs_dir() / f"{LOG_FILENAME_PREFIX}{stamp}.log"


class DailyFileSink:
    """按天写的文件 sink：文件名里带日期，跨天自动换文件，单文件超限就续编（``-2``、``-3``…）。

    为什么不用 loguru 自带的 ``rotation`` / ``retention``：

    - ``rotation`` 只能"改名 + 重开"，**改不了文件名** —— 跨天之后文件还叫前一天的名字，
      长开的窗口会把两天的记录写进同一个文件；
    - ``retention`` 只认它自己轮转出来的那些文件。

    我们要的是"一天一个文件、名字里就是日期"，与 ``console-<日期>.log`` 保持同一套规则；
    过期清理统一交给 :func:`purge_old_logs`（按修改时间，两类文件都扫得到）。

    ``clock`` 可注入，测试里用来模拟跨天（否则只能等到半夜）。
    """

    def __init__(
        self,
        directory: Path,
        *,
        prefix: str = LOG_FILENAME_PREFIX,
        max_bytes: int = ROTATION_SIZE_MB * 1024 * 1024,
        encoding: str = "utf-8",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._dir = Path(directory)
        self._prefix = prefix
        self._max_bytes = max_bytes
        self._encoding = encoding
        self._clock = clock or (lambda: time.strftime("%Y-%m-%d"))
        self._handle: object | None = None
        self._path: Path | None = None
        self._size = 0
        self._serial = 1

    # -- 对外：loguru 需要的三个方法 -------------------------------------------------
    @property
    def path(self) -> Path | None:
        """当前正在写的文件（还没写过时为 None）。"""
        return self._path

    def write(self, message: str) -> None:
        """loguru 每条日志调一次（``message`` 自带换行）。"""
        self._open_for_today()
        assert self._handle is not None
        data = message.encode(self._encoding, "replace")
        self._handle.write(data)  # type: ignore[attr-defined]
        self._handle.flush()  # type: ignore[attr-defined]
        self._size += len(data)
        if self._size >= self._max_bytes:
            self._roll_within_day()

    def stop(self) -> None:
        """loguru 移除 sink 时调用。"""
        self._close()

    def complete(self) -> None:
        """异步队列模式才用得上；同步模式下刷一下即可。"""
        if self._handle is not None:
            self._handle.flush()  # type: ignore[attr-defined]

    # -- 内部 -----------------------------------------------------------------------
    def _target(self) -> Path:
        stamp = self._clock()
        name = f"{self._prefix}{stamp}.log"
        if self._serial > 1:
            name = f"{self._prefix}{stamp}-{self._serial}.log"
        return self._dir / name

    def _open_for_today(self) -> None:
        target = self._target()
        if self._handle is not None and self._path == target:
            return
        self._close()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._handle = target.open("ab", buffering=0)  # 生命周期随 sink
        self._path = target
        try:
            self._size = target.stat().st_size
        except OSError:
            self._size = 0

    def _roll_within_day(self) -> None:
        """同一天里超过上限：换个序号继续（不覆盖，也不丢内容）。"""
        self._close()
        self._serial += 1
        self._open_for_today()

    def _close(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()  # type: ignore[attr-defined]
        self._handle = None


def console_file(day: str | None = None) -> Path:
    """当天的原生输出文件路径（``logs/console-YYYY-MM-DD.log``）。"""
    stamp = day or time.strftime("%Y-%m-%d")
    return paths.logs_dir() / f"{CONSOLE_PREFIX}{stamp}.log"


#: 受管子进程的日志组件名：文件名规则与主日志一致，``<组件>-YYYY-MM-DD.log``。
CHILD_LOG_COMPONENTS = ("worker", "qdrant")


def child_log_file(component: str, day: str | None = None, directory: Path | None = None) -> Path:
    """子进程日志的当天路径（``logs/worker-YYYY-MM-DD.log`` / ``qdrant-…``）。

    与 ``helper-<日期>.log`` / ``console-<日期>.log`` 同一套命名规则。
    子进程由 Supervisor 继承文件句柄直接写入（父进程不做转写），所以这里只负责
    "今天该写哪个名字"；``directory`` 允许测试/自定义目录（默认 ``logs/``）。
    """
    stamp = day or time.strftime("%Y-%m-%d")
    base = Path(directory) if directory is not None else paths.logs_dir()
    return base / f"{component}-{stamp}.log"


def init_child_logging(component: str, level: str = "DEBUG") -> None:
    """子进程（worker）专用日志初始化：**只装 stderr sink**，不写文件。

    为什么只装 stderr：子进程的 stdout/stderr 已被 Supervisor 重定向到
    ``logs/<组件>-<日期>.log``，这里再加文件 sink 就会双写。装 stderr 的目的只有一个——
    让它用同一套 :data:`LOG_FORMAT`（带 run / 模块:行），而不是 loguru 的默认格式，
    这样所有日志文件的字段顺序完全一致。
    """
    logger.remove()  # 清掉 loguru 默认 handler（它的格式与 LOG_FORMAT 不同）
    logger.configure(patcher=_patch)  # 兜底脱敏 + 保证 run/src/at 一定有值
    logger.add(  # 唯一的 sink：stderr（由 Supervisor 落到子日志文件）
        sys.stderr,
        level=level.upper(),  # 文件级别
        format=LOG_FORMAT,  # 与主日志同一套格式
        backtrace=False,  # 不带变量现场（防泄漏，见模块说明）
        diagnose=False,  # 异常栈不带局部变量
        catch=True,  # 日志自身出错不拖垮进程
    )
    logger.info("子进程日志已初始化：{}（输出到 stderr，由 Supervisor 落盘）", component)  # 起始标记


def init_logging(
    level: str = "INFO",
    *,
    log_file: Path | None = None,
    console_level: str = "WARNING",
    debug: bool = False,
    rotation: str | Callable[[str, object], bool] | None = None,
    retention: str | int = f"{RETENTION_DAYS} days",
) -> Path | None:
    """初始化进程日志（幂等：重复调用按新参数重建 sink）。

    参数：
        level: 文件 sink 的级别（``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``）。
            值来自引导配置 ``config.json`` 的 ``log_level``（技术方案 §12）。
        log_file: 日志文件路径；默认 ``{安装目录}/logs/helper.log``（测试可指定临时文件）。
        console_level: 控制台 sink 的级别，默认只放 WARNING 以上。
        debug: **调试模式**（界面上的开关）。打开时文件与控制台都降到 DEBUG——
            错误与每一步细节都会打到控制台（``sys.stderr`` → ``logs/console-<日期>.log``）。
        rotation: 轮转判据；默认"跨天或超过 20 MB"（见 :func:`_needs_rotation`）。
        retention: 保留策略，默认 7 天。
    返回：
        实际生效的日志文件路径；**文件写不进去时返回 None**（此时只有控制台 sink）。
    """
    global _log_file, _initialized

    if debug:
        level = "DEBUG"
        console_level = "DEBUG"

    # 先清掉旧 sink：不清的话重复初始化会让一行日志写两份。
    logger.remove()
    logger.configure(patcher=_patch)

    _log_file = None
    explicit_target = log_file
    daily: DailyFileSink | None = None
    # paths_seen 必须在 try **之前**定下来：目录建不出来时 except 分支还要用它报错
    if explicit_target is not None:
        paths_seen = explicit_target
    else:
        daily = DailyFileSink(paths.logs_dir())
        paths_seen = daily_log_file()
    # 公共参数（两类 sink 都收）
    common = {
        "level": level.upper(),
        "format": LOG_FORMAT,
        "enqueue": False,  # 受限令牌下建不了命名管道，异步队列会被拒；同步写足够快
        "backtrace": False,
        "diagnose": False,  # 见模块说明第 1 条：异常栈不许带局部变量
        "catch": True,  # 日志自身出错不该把业务代码带崩
    }
    try:
        paths_seen.parent.mkdir(parents=True, exist_ok=True)
        if daily is not None:
            # 正常启动：一天一个文件、名字里带日期（与 console-<日期>.log 同一套规则）。
            # 跨天换文件与单文件大小上限都由 DailyFileSink 自己处理，所以这里不传那些文件专用参数。
            logger.add(daily, **common)
        else:
            # 明确给了路径（测试、脚本）：按单文件写。**encoding/rotation/retention 只能给文件路径**，
            # 给别的类型 loguru 会直接拒绝——所以两类 sink 的参数是分开传的。
            logger.add(
                str(paths_seen),
                encoding="utf-8",
                rotation=rotation if rotation is not None else _needs_rotation,
                retention=retention,
                **common,
            )
        _log_file = paths_seen
    except OSError as exc:
        # 退回控制台：装到只读位置时至少还能看到告警，而不是直接起不来。
        logger.add(sys.stderr, level=console_level.upper(), backtrace=False, diagnose=False)
        logger.warning("日志文件不可用（{}），本次只输出到控制台：{}", paths_seen, exc)
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
    logger.debug("日志已初始化：{}（级别 {}）", paths_seen, level.upper())
    return paths_seen


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
        # bind 真实来源：截获来的记录没有 Python 帧，{name}/{line} 会是 None（显示成 None:0）
        logger.bind(src=record.name, at=record.lineno).opt(
            depth=depth, exception=record.exc_info
        ).log(level, record.getMessage())


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


def _asyncio_exception_handler(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """事件循环里的未处理异常 → 日志（含"Task exception was never retrieved"这一类）。"""
    exception = context.get("exception")
    message = str(context.get("message") or "asyncio 未处理异常")
    # 已知噪音，降到 DEBUG：Windows 上对端（浏览器/渲染进程）突然断开连接时，
    # ProactorEventLoop 会在 _call_connection_lost 里抛 ConnectionResetError。
    # 窗口关闭、页面重载都会触发它，属于正常现象；渲染进程真崩了我们另有
    # renderProcessTerminated 记着（见 __main__），所以这里不必占 ERROR。
    if isinstance(exception, ConnectionResetError) or "_call_connection_lost" in message:
        logger.opt(exception=exception).debug("事件循环里的连接重置（正常现象）：{}", message)
        return
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


def _broken_standard_fds() -> set[int]:
    """**在 dup2 之前**判断 1/2 号 fd 是否可用。

    为什么要赶在 dup2 之前：dup2 会把它们接好，之后再 fstat 永远成功——而 Python 启动时
    创建的标准流对象内部缓存的是**启动那一刻的句柄**，句柄坏过的对象即使 fd 后来被接好，
    写起来照样报 ``OSError: 句柄无效``（实测踩过：判断晚了就修不好 print）。
    """
    broken: set[int] = set()
    for index in (1, 2):
        try:
            os.fstat(index)
        except OSError:
            broken.add(index)
    return broken


def _ensure_valid_streams(_broken: set[int]) -> None:
    """把 ``sys.stdout`` / ``sys.stderr`` 换成绑定到**我们自己的 dup fd** 的 UTF-8 流。

    **无条件换，不做"能 reconfigure 就复用"**——这一条是被线上故障逼出来的：

    Windows 上标准流对象内部缓存了**进程启动那一刻的 OS 句柄**。双击启动（没有控制台）时那是无效句柄，
    可是 ``os.dup2`` 把 CRT fd 接到文件上之后，``os.fstat(1)`` 会报成功、``reconfigure`` 也不报错，
    **唯独 write 仍然失败**（``OSError: [WinError 6] 句柄无效``）。后果实测过：
    NiceGUI 的 ``welcome.collect_urls()`` 里那句 ``print('NiceGUI ready to go on ...')`` 抛错 →
    lifespan 启动失败 → ``uvicorn sys.exit(3)`` → 表现正是「界面服务 20 秒没起来 + 窗口白板」。

    替身流必须**自己 ``os.dup`` 一个 fd**（不能直接包 fd 1/2）：否则原对象被回收时 ``close()`` 会把
    fd 1 关掉，那个 fd 立刻被 loguru 的文件 sink 拿走 → ``print`` 的输出跑进主日志、退出码还会变成 120。

    编码统一 UTF-8 之后，调用方还要**重建 loguru 的控制台 sink**（否则它还握着旧流，用 GBK 写 → 乱码）。
    """
    global _stdout_keepalive, _stderr_keepalive

    for index in (1, 2):
        replacement = os.fdopen(
            os.dup(index), "w", encoding="utf-8", errors="replace", buffering=1
        )
        if index == 1:
            _stdout_keepalive = replacement
            sys.stdout = replacement
        else:
            _stderr_keepalive = replacement
            sys.stderr = replacement


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
    broken = _broken_standard_fds()  # 必须在 dup2 之前探（见 _broken_standard_fds 的说明）
    if broken:
        logger.warning("启动时标准句柄就是坏的（fd {}）——多半是双击启动（没有控制台）", sorted(broken))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("ab", buffering=0)  # 这个句柄要和进程同寿
        os.dup2(handle.fileno(), 1)
        os.dup2(handle.fileno(), 2)
        # 两个流必须可用且是 UTF-8，否则双击启动（没有控制台、句柄无效）时任何 print 都会抛
        # `OSError: 句柄无效` —— 实测它让 NiceGUI 的服务线程在打就绪横幅时**直接死掉**，
        # 表现正是"界面服务 20 秒没起来 + 窗口白板"。
        _ensure_valid_streams(broken)
    except OSError as exc:
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


def is_initialized() -> bool:
    """日志系统是否已初始化（启动自检用）。"""
    return _initialized


def log_file() -> Path | None:
    """当前日志文件路径；只有控制台 sink 时返回 None。"""
    return _log_file
