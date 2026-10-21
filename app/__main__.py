"""入口：``python -m app``（等价于 `helper` 控制台脚本）。

**窗口壳为什么不是 NiceGUI 自带的 native 模式**：native 模式走 pywebview，而 pywebview 在
Windows 上要经 pythonnet 加载 .NET，本机上 CLR 加载直接失败
（``Failed to resolve Python.Runtime.Loader.Initialize``，netfx / coreclr 两种模式都试过）。
改用 **PySide6 的 QtWebEngine** 承载同一套 NiceGUI 界面：界面代码一行不改，
也不依赖 .NET 或系统的 WebView2 运行时。

**"白板"是怎么来的、这里怎么防**（每条都是实测踩过的）：

1. **别只等 TCP 端口**：uvicorn 先绑定端口、之后才准备就绪，这时 load 会拿到空页面；
   要等**真的能取到 HTTP 200**，再多留一点时间让它稳下来。
2. **启动参数**：受限环境里 Chromium 的渲染沙箱起不来，渲染进程直接失败、``loadFinished``
   为 False、DOM 长度 0 —— 表现就是一片白。实测：不带参数 = 白板、只带 ``--disable-gpu`` = 白板、
   ``--disable-gpu --no-sandbox --disable-dev-shm-usage`` + ``QTWEBENGINE_DISABLE_SANDBOX=1`` = 正常。
   我们的 WebView 只加载 ``127.0.0.1`` 上自己进程提供的页面，这个取舍可接受。
3. **加载失败要重试，而且要确认"内容真的到了"**：NiceGUI 的页面在 WebSocket 连上之前只是空壳，
   只看 ``loadFinished`` 不够——还要查一次 DOM 里有没有根节点，没有就重载。
4. **失败必须让用户看得见**：启动前提不满足（目录建不出来 / 库损坏 / 迁移缺脚本）、
   界面服务没起来、窗口反复白板——这三种以前都只写日志，而双击启动没有常驻控制台，
   用户看到的就是"闪一下"或一个空窗口。现在都会写进 ``logs/startup-error.log`` 并弹一个提示框。
"""

from __future__ import annotations

import atexit
import contextlib
import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

#: 退出阶段标志：on_quit 一开始就置真。界面服务线程在 shutdown 时可能抛
#: wsproto 的 LocalProtocolError / asyncio 的 CancelledError——这是正常退出竞态，
#: 不能按“服务线程异常退出”报 CRITICAL 或写 startup-error。
_QUIT = threading.Event()

#: QtWebEngine 的启动参数。**这三项是实测出来的，不是照抄的**（见模块说明第 2 条）。
#: 末尾的 `--log-level=3` 是**降噪**用（2026-10 加）：软件渲染下 Chromium 的 GPU 进程必然初始化
#: 失败并打两行 `Failed to create GLES3 context` / `ContextResult::kFatalFailure` —— 这是**预期**的
#: （我们本来就强制 `--disable-gpu`），压到只留 FATAL，免得真正的报错被淹掉（FATAL 仍会打印）。
#:
#: ⚠ **前提：运行窗口（以及 `scripts/check_ui.py`）必须在不受限的会话里**。受限沙箱下 QtWebEngine
#: 的渲染进程间通信建不了命名管道，表现是**一片白板**（日志里不会有"界面已渲染"这一行，
#: stderr 会有 `Check failed: . : 拒绝访问 (0x5)`）。这不是代码 bug，别去改 flag 找原因。
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --no-sandbox --disable-dev-shm-usage --log-level=3",
)
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

# 监听地址来自引导配置（``config.json`` 的 ``host``），默认回环 —— 见 :func:`_listen_host`。
# 历史：这里曾经硬编码 ``HOST = "127.0.0.1"``；配置文件用起来之后挪进了配置。

#: 等界面服务就绪的上限（秒）；超时也照样把窗口开出来，但会弹提示告诉用户去看日志。
SERVER_WAIT_S = 20.0

#: 服务就绪后再等一会儿让 uvicorn 完全稳下来（避免"端口通了但请求还失败"的窗口期）。
SETTLE_S = 0.4

#: 加载失败 / 内容没渲染时最多重载几次。
MAX_RELOAD = 3
RELOAD_DELAY_MS = 800

#: 端口被抢时最多换几个端口重试。`_free_port` 是"取完就放手"的（bind→close→交给 uvicorn），
#: 中间存在 TOCTOU：另一个进程恰好在那一瞬间占用了它，uvicorn 会 SystemExit(3)。
#: 桌面应用不该因为这种偶发竞态就打不开，所以换一个端口重试。
SERVER_PORT_RETRIES = 3

#: 初始窗口与屏幕边缘的留白（像素）：和常见桌面软件一样，不贴边。
WINDOW_MARGIN = 48

#: 初始窗口的最小尺寸（屏幕很小时也不至于缩成一条）。
WINDOW_MIN_SIZE = (860, 580)

#: 启动失败的记录文件名（先写 logs/，写不进去再写安装目录）。
STARTUP_ERROR_FILE = "startup-error.log"


#: 收尾行只记一次（Qt 的 aboutToQuit 与 atexit 都会来叫它）。
_run_summary_logged = False


def _log_run_summary(started_monotonic: float, started_wall: float) -> None:
    """记一行「本次运行结束」（幂等）。

    **有这一行才说明是用户正常关窗**；进程崩掉时不会有——这是日志里唯一能区分
    「正常退出」与「挂掉」的信号（以前只能靠有没有「已记住窗口几何」反推）。

    除了 Qt 的 ``aboutToQuit``，还挂到 ``atexit``：现实里退出可能走别的路径
    （实测：从外部发 WM_CLOSE 到控制台窗口时 Qt 的退出序列根本不参与），这行不该因此丢掉。
    ``atexit`` 只在**正常结束**时跑，崩溃不会触发——正好符合我们要的语义。
    """
    global _run_summary_logged
    if _run_summary_logged:
        return
    _run_summary_logged = True
    try:
        from loguru import logger

        from app.core.jobs import store as jobs_store
        from app.core.logging import RUN_ID

        done = jobs_store.count_jobs(statuses=("done",), finished_since=started_wall)
        failed = jobs_store.count_jobs(statuses=("failed",), finished_since=started_wall)
        logger.info(
            "本次运行结束：run={} 时长 {:.1f} 分钟；任务完成 {} 条、失败 {} 条",
            RUN_ID,
            (time.monotonic() - started_monotonic) / 60,
            done,
            failed,
        )
    except Exception as exc:
        from loguru import logger

        logger.warning("运行收尾统计失败（不影响退出）：{}", exc)


#: Qt 消息级别映射（按枚举名，便于测试）。
_QT_LEVELS: dict[str, str] = {
    "QtDebugMsg": "DEBUG",
    "QtInfoMsg": "INFO",
    "QtWarningMsg": "WARNING",
    "QtCriticalMsg": "ERROR",
    "QtFatalMsg": "CRITICAL",
}

#: **已知无害、几乎每次启动都会出现**的 Qt 消息：我们强制软件渲染（``--disable-gpu``），
#: 这类消息是必然结果（渲染本身没问题——日志里那句「界面已渲染」就是证据）。
#: 降到 DEBUG：不丢证据，但默认级别（INFO）下不再占地方、不再淹没真报错。
#: **只按整条文本精确匹配**，不做前缀/模糊匹配——模糊匹配会误伤真正的告警。
_QT_KNOWN_HARMLESS: frozenset[str] = frozenset({
    "GPUInfo not initialized on GpuInfoUpdate",
})


def _qt_message_level(mode_name: str, message: str) -> str:
    """Qt 消息 → 日志级别（纯逻辑，便于测试）。"""
    if message.strip() in _QT_KNOWN_HARMLESS:
        return "DEBUG"
    return _QT_LEVELS.get(mode_name, "INFO")


def _js_log_level(enum_name: str) -> str:
    """浏览器控制台级别 → 日志级别。

    ``InfoMessageLevel`` 降到 DEBUG：前端 ``console.log`` 的量很大，压到 DEBUG 之后默认级别
    （INFO）就不记了，只留真正要看的 warning / error。
    """
    return {
        "InfoMessageLevel": "DEBUG",
        "WarningMessageLevel": "WARNING",
        "ErrorMessageLevel": "ERROR",
    }.get(enum_name, "DEBUG")


def _install_qt_message_handler() -> None:
    """把 Qt 自己的 warning/critical 送进日志（技术方案 §10.1 的"渲染"层）。

    为什么放在这里而不是 ``core/logging``：core 不许 import 界面库（红线 1），
    而 ``qInstallMessageHandler`` 必须 import ``PySide6.QtCore``。
    """
    from loguru import logger as qt_logger
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    def handler(mode: QtMsgType, context: object, message: str) -> None:  # noqa: ARG001
        if "can't open file for write" in message:
            # Chromium 退出时写自己的日志文件失败（常发生在 debug.log 被占用/已删）。
            # 这是纯退出噪音，不影响数据；不再当作 Qt 告警写进日志。
            return
        name = getattr(mode, "name", None) or str(mode)
        qt_logger.bind(src="Qt", at=0).opt(depth=1).log(
            _qt_message_level(name, message), "Qt: {}", message
        )

    qInstallMessageHandler(handler)


def _is_allowed_navigation(url: str, allowed_origin: str) -> bool:
    """主框架导航是否允许：**只允许应用自己的 origin**（外加 data/blob/about 内部页）。

    为什么必须有这道闸：markdown 渲染出的 ``<a href>`` 没有 target，QtWebEngine 默认
    **在同一个视图里导航**——点一条外链，整个应用界面就被外站页面替换了（实测）。
    外链应该交给系统浏览器，内置视图永远留在应用里。

    参数：
        url: 目标地址（``QUrl.toString()``）。
        allowed_origin: 应用自己的 origin，如 ``http://127.0.0.1:8080/``。
    返回：
        允许导航返回 True；外链返回 False（调用方负责改交系统浏览器）。
    """
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme in ("data", "blob", "about"):
        return True

    def _port(parts: object) -> int | None:
        scheme = getattr(parts, "scheme", "")
        try:
            port = getattr(parts, "port", None)
        except ValueError:
            # 非法端口（如 ``127.0.0.1:8080.evil.com``）→ 不认作同源
            return None
        if port is not None:
            return int(port)
        return 443 if scheme == "https" else 80 if scheme == "http" else None

    base = urlsplit(allowed_origin)
    if parsed.scheme not in ("http", "https") or base.scheme not in ("http", "https"):
        return False
    return parsed.hostname == base.hostname and _port(parsed) == _port(base)


def _logging_page_class(allowed_origin: str) -> type:
    """造一个 QWebEnginePage 子类：**控制台消息进日志 + 挡住外链导航**。

    为什么需要控制台日志：界面是跑在 Chromium 里的，前端自己的报错（脚本异常、Vue 警告、
    资源 404）只在浏览器控制台里——服务端一行都看不到。

    为什么需要导航闸：见 :func:`_is_allowed_navigation`。外链一律交系统默认浏览器，
    内置视图不会离开应用；``target=_blank`` 走 :meth:`createWindow` 的同一套逻辑。
    """
    from loguru import logger as js_logger
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWebEngineCore import QWebEnginePage

    class LoggingPage(QWebEnginePage):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self._allowed_origin = allowed_origin
            #: ``target=_blank`` 时 Qt 会要一个新 page；拿住引用防被 GC
            self._popups: list[QWebEnginePage] = []

        def javaScriptConsoleMessage(self, level, message, line_number, source_id) -> None:  # type: ignore[no-untyped-def]
            name = getattr(level, "name", None) or str(level).rsplit(".", 1)[-1]
            js_logger.bind(src=source_id or "js", at=line_number).opt(depth=1).log(
                _js_log_level(name), "浏览器控制台：{}", message
            )

        def acceptNavigationRequest(self, url, _navigation_type, is_main_frame) -> bool:  # type: ignore[no-untyped-def]
            if not is_main_frame:
                return True
            target = url.toString()
            if _is_allowed_navigation(target, self._allowed_origin):
                return True
            js_logger.info("外链交给系统浏览器：{}", target)
            QDesktopServices.openUrl(url)
            return False

        def createWindow(self, _window_type) -> QWebEnginePage:  # type: ignore[no-untyped-def]
            # ``target=_blank``：新建一个只负责拦截的 page，真正的 URL 到达时由
            # ``acceptNavigationRequest`` 交系统浏览器并拒绝导航。
            popup = LoggingPage(self.profile(), self)
            self._popups.append(popup)
            return popup

    return LoggingPage


def _install_log_channels() -> None:
    """四个通道里属于"入口"的那些（技术方案 §10.2）。

    顺序不能改：**fd 重定向必须在 QtWebEngine 初始化之前**，否则 Qt 的启动输出就漏掉了。
    stdlib 桥要装在这里而不是 ``bootstrap``：它会动 root logger，测试进程里跑会劫走 pytest 的输出。
    """
    from loguru import logger as entry_logger

    from app.core.config import access
    from app.core.logging import (
        capture_native_output,
        init_logging,
        install_asyncio_handler,
        install_stdlib_bridge,
    )

    debug = access.load().log_debug
    if debug:
        # **调试模式**：不重定向原生输出——保留真实控制台，让错误/细节直接在终端里可见。
        # 代价是 Qt/Chromium 的 C 层输出不再进 console 日志（排障现场少了；这就是调试模式的含义）。
        console = None
        init_logging("DEBUG", debug=True)
    else:
        console = capture_native_output()      # ① 原生输出（fd 1+2）→ logs/console-<日期>.log
        # 捕获会把 sys.stdout/stderr 换成新流（绑定到刚接好的 fd），所以日志的**控制台 sink 要重建**：
        # 否则它还握着旧流——旧流编码是 GBK（console 文件里中文变乱码），且可能已经是坏句柄。
        init_logging(access.load().log_level)
    install_stdlib_bridge()                    # ② stdlib logging（NiceGUI/uvicorn/asyncio/PySide6）
    install_asyncio_handler()                  # ③ asyncio 任务异常（界面回调）
    _install_qt_message_handler()              # ④ Qt 自己的消息
    if console is not None:
        entry_logger.info("原生输出已捕获：{}", console)


def _listen_host() -> str:
    """界面服务的监听地址（来自数据库 ``bootstrap.host``）。

    默认回环。**非回环默认拒绝启动**：界面没有任何认证，绑到 ``0.0.0.0``/局域网 IP
    等于同网段任何人都能打开它、看资料、改设置。确实需要（比如临时给同事看）时，
    设置环境变量 ``HELPER_ALLOW_INSECURE_HOST=1`` 显式承担这个风险，启动日志会留痕。

    异常：
        RuntimeError: 配了非回环地址且没有显式放行。
    """
    from loguru import logger

    from app.core.config import access
    from app.core.config.schema import is_loopback

    host = access.load().host
    if is_loopback(host):
        return host
    if os.environ.get("HELPER_ALLOW_INSECURE_HOST") == "1":
        logger.warning(
            "监听地址不是回环（{}），且已由 HELPER_ALLOW_INSECURE_HOST=1 放行："
            "界面没有认证，同一网络里任何人都能打开它。",
            host,
        )
        return host
    raise RuntimeError(
        f"监听了非回环地址 {host}：界面没有认证，同网段任何人都能打开它。\n"
        "默认值是 127.0.0.1；确实要对外提供访问，请设置环境变量 "
        "HELPER_ALLOW_INSECURE_HOST=1 后再启动。"
    )


def _free_port(host: str) -> int:
    """向系统要一个空闲端口（桌面应用不该因为端口被占就起不来）。

    注意要**按配置里的监听地址**去要：绑 ``127.0.0.1`` 拿到的端口在别的网卡上未必空闲。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _probe_bind(host: str, port: int) -> str:
    """再试着绑一次同一个端口，把结论写成一句人话。

    为什么要它：uvicorn 启动失败时只抛 ``SystemExit(3)``，而**原因会被 uvicorn 自己的
    logging 配置吞掉**（实测：``uvicorn.error`` 的 handler 被它的 dictConfig 清空，消息哪儿都不去）。
    于是"端口被占"和"别的原因"在日志里长得一模一样。绑一次立刻就能区分，成本可忽略。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return f"端口 {host}:{port} 现在可以绑定 → 刚才是别的原因（不是端口被占）"
    except OSError as exc:
        return f"端口 {host}:{port} 现在绑不上（{exc}）→ 大概率是端口被占/被系统保留"


def _place_window(view: object, screen: object, preferred: tuple[int, int]) -> tuple[int, int]:
    """把窗口放到**主屏可用区域的正中**（每次打开都一样，不记住上次几何）。

    为什么不再"记住窗口几何"：外接显示器拔掉、分辨率/缩放变化之后，记下的位置可能落在
    屏幕外，窗口"打开就不见了"；而窗口位置本来就不该是用户要维护的状态。固定居中 + 合理
    尺寸，和大多数桌面软件的默认行为一致。

    尺寸优先 ``preferred``；屏幕比它小就按 :data:`WINDOW_MARGIN` 留白收敛，
    但不小于 :data:`WINDOW_MIN_SIZE`（避免小屏上缩成一条）。

    返回：实际采用的 ``(宽, 高)``。
    """
    preferred_w, preferred_h = preferred
    if screen is None:  # 无头/受限会话：拿不到屏幕信息，退回固定尺寸
        view.resize(preferred_w, preferred_h)  # type: ignore[attr-defined]
        return preferred_w, preferred_h

    available = screen.availableGeometry()  # type: ignore[attr-defined]
    min_w, min_h = WINDOW_MIN_SIZE
    width = min(preferred_w, max(min_w, int(available.width()) - WINDOW_MARGIN * 2))
    height = min(preferred_h, max(min_h, int(available.height()) - WINDOW_MARGIN * 2))
    view.resize(width, height)  # type: ignore[attr-defined]
    view.move(  # type: ignore[attr-defined]
        int(available.x()) + (int(available.width()) - width) // 2,
        int(available.y()) + (int(available.height()) - height) // 2,
    )
    return width, height



def _wait_http(
    url: str | Callable[[], str],
    timeout_s: float = SERVER_WAIT_S,
    *,
    report_every_s: float = 5.0,
) -> bool:
    """等界面服务真的返回 HTTP 200（不是只等端口）。

    ``url`` 可以是**取值函数**：端口被抢时服务线程会换端口重试，这里每一轮重新取一次。
    """
    from loguru import logger

    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    next_report = report_every_s
    while time.monotonic() < deadline:
        target = url() if callable(url) else url
        try:
            with urllib.request.urlopen(target, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.15)
        waited = time.monotonic() - started
        if waited >= next_report:
            next_report += report_every_s
            logger.warning('界面服务还没就绪（已等 {:.0f}s）：{}', waited, target)
    return False


def _tail_of(path: Path, lines: int = 40) -> str:
    """取文件末尾若干行（日志可能很大，不整份读进来）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _write_startup_error(title: str, detail: str) -> Path | None:
    """把启动失败写进一个**用户找得到**的文件，返回实际写入的路径。

    为什么不只靠日志：日志系统本身可能正是失败的那一环（目录不可写、坏配置隔离发生在
    建 sink 之前），所以这里不依赖任何前提，直接写文件；两个位置都写不进去才放弃。

    **从 2026-10 起还附上两样东西**（技术方案 §10.5 的 S3）：受限环境的诊断结论，以及
    **原生输出的尾巴** —— 白板那类问题的现场（Qt/Chromium 的 ``FATAL``）只在
    ``console-<日期>.log`` 里，而用户报障时最该看到的就是它。
    """
    from app.core import paths
    from app.core.logging import RUN_ID, console_file, console_output_file, sniff_restricted_hint

    lines = [f"helper 启动失败：{title}", f"run：{RUN_ID}", ""]
    lines += [f"{name}：{value}" for name, value in paths.describe().items()]
    lines += ["", detail]

    hint = sniff_restricted_hint()
    if hint:
        lines += ["", "诊断：" + hint]

    console = console_output_file() or console_file()
    tail = _tail_of(console) if console else ""
    if tail.strip():
        lines += ["", f"原生输出最后几行（{console}）：", "-" * 60, tail]

    text = "\n".join(lines)

    for target in (paths.logs_dir() / STARTUP_ERROR_FILE, paths.project_root() / STARTUP_ERROR_FILE):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            return target
        except OSError:
            continue
    return None


def _show_box(icon: str, title: str, message: str) -> None:
    """弹一个 Qt 提示框（启动失败与"界面没起来"共用）。

    参数：
        icon: ``critical`` 或 ``warning``。
        title: 窗口标题。
        message: 正文（要给用户可执行的下一步，比如"日志在哪"）。
    """
    from PySide6.QtWidgets import QApplication, QMessageBox

    QApplication.instance() or QApplication(sys.argv)
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical if icon == "critical" else QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(message)
    box.exec()


def main() -> int:
    """启动桌面窗口（阻塞到窗口关闭）。

    为什么长（约 260 行，R2 例外：**入口函数的顺序编排**）：它把启动的每一步按
    **必须的、不可调换的顺序**串起来——自检（须先于 Qt import）→ 单实例守卫 →
    ensure_ready（目录/日志/库）→ 任务系统崩溃恢复 → 窗口与 ui.page → 退出清理。
    每步都对应一个真实故障（见技术方案 §11/§12），强行拆散成若干小函数反而会让
    "谁先谁后"藏在调用图里。已有 `app/bootstrap.py` 承接目录/日志/建表，这里只做编排。
    """
    run_started_monotonic = time.monotonic()
    run_started_wall = time.time()
    atexit.register(_log_run_summary, run_started_monotonic, run_started_wall)
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from loguru import logger

    from app.bootstrap import ensure_ready
    from app.core import paths

    # T2.3 自检命令：**必须在 import Qt 之前**接线（自检不需要 Qt，受限会话里也要能跑）。
    # 也不用走 single_instance 守卫（它就是排查环境用的）。
    if "--selfcheck" in sys.argv:
        from app.selfcheck import main as selfcheck_main

        return selfcheck_main(json_output="--json" in sys.argv, deep_hash="--no-hash" not in sys.argv)

    # 单实例守卫（T2.1，只在 main() 里做；测试 / 脚本 / --selfcheck 都不走这里，不会被挡）。
    # 放在 ensure_ready() 之前：已有实例时根本不该再初始化目录/日志/数据库。
    from app.core import single_instance

    if not single_instance.acquire():
        try:
            activated = single_instance.activate_existing_window()
            _show_box(
                "warning",
                "helper",
                "helper 已经在运行了（已把它的窗口切到前面）。"
                if activated
                else "helper 已经在运行了。",
            )
        except Exception as exc:
            logger.warning("单实例提示未能弹出：{}", exc)
        return 0

    # 启动前提不满足（目录建不出来 / 库损坏 / 迁移缺脚本）时**必须让用户看见**：
    # 双击启动没有常驻控制台，以前这种情况就是"闪一下、什么都没有"。
    try:
        ensure_ready()
    except Exception as exc:
        where = _write_startup_error(f"{type(exc).__name__}: {exc}", traceback.format_exc())
        _show_box(
            "critical",
            "helper 启动失败",
            f"{type(exc).__name__}: {exc}\n\n详细信息已写入：\n{where or '（文件也写不进去）'}",
        )
        return 2

    from nicegui import ui

    from app import tasks as tasks_app
    from app.nicegui_patch import skip_unused_process_pool
    from app.ui.shell import WINDOW_SIZE, build_shell, set_window_width

    # 去掉 NiceGUI 那个我们用不到的进程池（它在受限环境下会因命名管道直接失败，见模块说明）
    skip_unused_process_pool()

    # 任务系统与 Qdrant 服务端的生命周期**绑定主程序**：
    # - ``vector.url`` 为空：本地嵌入式（兼容模式），Runner 在主进程；
    # - ``vector.url`` 非空：启动 Qdrant sidecar + worker，主进程不再跑 Runner；
    # - 退出时由 on_quit 统一 stop（见下），不会留下独立 Qdrant/worker 进程。
    from app.services import services as services_service

    startup_service_notes: list[str] = []
    try:
        services_service.start()
    except Exception as exc:
        # 本机服务（Qdrant / worker）起不来的场景以前只写日志，双击启动用户根本看不到
        # （"闪一下、什么都没有"）。这里走和 ensure_ready 一致的路径：落文件 + 弹窗。
        logger.opt(exception=exc).critical('本机服务启动失败：{}', exc)
        where = _write_startup_error(f'本机服务启动失败：{type(exc).__name__}: {exc}', traceback.format_exc())
        _show_box(
            "critical",
            "helper 启动失败",
            f"后台服务启动失败：{type(exc).__name__}: {exc}\n\n详细信息已写入：\n{where or '（文件也写不进去）'}",
        )
        return 2
    # 服务进程可能"起了但没健康"（Qdrant 10 秒没就绪 / worker 反复崩溃降级）。这些场景
    # 以前只写日志；把它们收集起来，稍后随 warnings 一起弹给用户。
    try:
        for name, child in services_service.status().items():
            if not isinstance(child, dict):
                continue
            if child.get("degraded"):
                startup_service_notes.append(
                    f'后台进程 {name} 反复崩溃已被降级，可能不可用。\n'
                    f'日志位置：{paths.data_dir() / "logs"}'
                )
            elif name == "qdrant" and child.get("running") is False and child.get("installed"):
                startup_service_notes.append(
                    'Qdrant 服务没有就绪（可能端口被占或启动失败）。\n'
                    f'日志位置：{paths.data_dir() / "logs"}'
                )
    except Exception as exc:
        logger.warning('启动后检查后台服务状态失败（不影响启动）：{}', exc)
    if services_service.worker_enabled():
        # 服务端已经就绪：现在才建/迁移集合（bootstrap 阶段故意跳过，见 ensure_ready 说明）。
        try:
            from app.core.vector import api as vector_api

            vector_api.ensure_collections()
        except Exception as exc:
            logger.warning('服务端向量集合自检失败（检索/导入会另行报错）：{}', exc)
        # 首次从本地嵌入式切服务端：服务端库是空的，自动排一轮重建，避免"资料在、搜索为空"。
        try:
            tasks_app.register_kinds()
            from app.services.knowledge.queue import migrate_server_store_if_needed

            migrated = migrate_server_store_if_needed()
            if migrated:
                logger.info('服务端首次迁移：已排队重建 {} 份资料', migrated)
        except Exception as exc:
            logger.warning('服务端首次迁移检查失败（不影响启动）：{}', exc)
        logger.info('任务执行在独立 worker 子进程（主进程不跑 Runner）')
    else:
        tasks_app.start()

    @ui.page('/')
    def index() -> None:
        build_shell()

    _install_log_channels()

    try:
        host = _listen_host()
    except RuntimeError as exc:
        where = _write_startup_error('监听地址不安全', str(exc))
        _show_box('critical', 'helper 拒绝启动', f'{exc}\n\n详细信息已写入：\n{where or "（文件也写不进去）"}')
        return 2
    # 端口与 url 放一个可变字典里：服务线程可能因端口被抢而换端口重试，主线程要跟着读最新的。
    state: dict[str, object] = {"port": _free_port(host), "attempts": 0, "url": ""}
    port_chosen = threading.Event()
    warnings: list[str] = [
        # 起步阶段收集的后台服务异常，随界面就绪一起弹给用户。
        *startup_service_notes,
    ]

    def current_url() -> str:
        """当前服务地址（端口重试后会变）。"""
        return str(state["url"] or f'http://{host}:{state["port"]}/')

    def serve() -> None:
        """界面服务线程的入口。

        **为什么要包一层函数而不是直接 lambda**：这一层以前是"哑"的——服务起不来时日志里
        只有 20 秒后的一句"未就绪"，看不出是抛了异常、还是卡住了、还是退出了（实测踩过：
        无控制台启动时偶发卡住，没有任何证据）。现在它把"开始 / 正常退出 / 异常退出"都记下来。

        **端口重试**：``_free_port`` 取完端口就放手，到 uvicorn 真正绑定之间有个极小窗口
        可能被别的进程抢走（TOCTOU）。命中时 uvicorn 抛 ``SystemExit(3)``；桌面应用不该
        因此打不开，所以换一个端口再试（最多 :data:`SERVER_PORT_RETRIES` 次）。
        """
        while True:
            port_now = int(state["port"])
            state["url"] = f'http://{host}:{port_now}/'
            port_chosen.set()
            logger.info('界面服务线程启动：{}:{}', host, port_now)
            try:
                ui.run(
                    host=host,
                    port=port_now,
                    title='helper',
                    favicon='🧩',
                    show=False,     # 明确：不许打开浏览器
                    reload=False,   # 桌面窗口绝不能热重载
                    native=False,   # 窗口由下面的 QtWebEngine 提供
                    dark=False,     # 只做浅色（DSH 浅色色板）
                )
            except SystemExit as exc:
                # uvicorn 启动失败会 sys.exit(3)，而且**失败原因被它自己的 logging 配置吞掉**
                # （实测：uvicorn.error 的 handler 被 dictConfig 清空）。所以这里自己补上。
                attempts = int(state["attempts"]) + 1
                state["attempts"] = attempts
                if attempts < SERVER_PORT_RETRIES:
                    new_port = _free_port(host)
                    logger.warning(
                        '界面服务在 {}:{} 启动失败（SystemExit {}），换端口 {} 重试（第 {} 次）',
                        host, port_now, exc.code, new_port, attempts,
                    )
                    state["port"] = new_port
                    port_chosen.clear()
                    continue
                logger.opt(exception=exc).critical(
                    '界面服务启动失败（SystemExit {}）：{}:{}', exc.code, host, port_now
                )
                logger.error('端口自检：{}', _probe_bind(host, port_now))
                _write_startup_error(
                    '界面服务没能起来',
                    f'uvicorn 以 SystemExit({exc.code}) 退出；地址 {host}:{port_now}\n'
                    f'端口自检：{_probe_bind(host, port_now)}',
                )
                return
            except BaseException as exc:
                # 退出阶段：uvicorn 的 websocket 已经被浏览器关闭，shutdown 再发
                # CloseConnection 会抛 wsproto.LocalProtocolError；lifespan 也可能
                # 抛 CancelledError。这是正常关闭竞态，不是服务崩溃。
                if (
                    _QUIT.is_set()
                    or type(exc).__name__ in {"LocalProtocolError", "CancelledError"}
                ):
                    logger.debug('界面服务在退出阶段结束：{}', exc)
                    return
                logger.opt(exception=exc).critical('界面服务线程异常退出：{}', exc)
                _write_startup_error(
                    '界面服务线程异常退出',
                    f'{type(exc).__name__}: {exc}\n地址 {host}:{port_now}',
                )
                return
            logger.warning('界面服务线程已退出（服务不再可用）')
            return

    # 关闭阶段浏览器会断开 keep-alive 连接，uvicorn 的 Proactor transport 会打一条
    # ConnectionResetError 的 asyncio 异常（WinError 10054）。这是关闭时的正常现象，
    # 不是数据/服务故障；在本应用自己的 loop 上过滤掉，避免用户被 traceback 吓到。
    try:
        from nicegui import app as _nicegui_app

        @_nicegui_app.on_startup
        def _quiet_transport_reset() -> None:
            import asyncio

            loop = asyncio.get_running_loop()
            previous = loop.get_exception_handler()

            def _handler(loop: object, context: dict) -> None:
                exc = context.get("exception")
                if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
                    logger.debug('忽略关闭阶段的连接重置：{}', exc)
                    return
                if previous is not None:
                    previous(loop, context)  # type: ignore[arg-type]
                else:
                    loop.default_exception_handler(context)  # type: ignore[attr-defined]

            loop.set_exception_handler(_handler)

    except Exception as exc:
        logger.debug('安装连接重置过滤器失败（不影响使用）：{}', exc)

    # 界面服务跑在后台线程：主线程必须留给 Qt 的事件循环（窗口只能由主线程创建）。
    _server = threading.Thread(target=serve, name='nicegui-server', daemon=True)
    _server.start()
    # 等服务线程先选定端口（它可能已经换过一次），再去探 HTTP
    port_chosen.wait(timeout=5.0)

    from app.core.logging import daily_log_file

    log_path = daily_log_file()
    if _wait_http(current_url):
        url = current_url()
        logger.info('界面服务就绪：{}', url)
        time.sleep(SETTLE_S)
        # **uvicorn 起服务时会重设自己的 logging 配置**，把 stdlib bridge 覆盖掉；
        # 这里再装一次，兑现"四通道全收"（见 handlers.install_stdlib_bridge 的注释）。
        try:
            from app.core.logging import install_stdlib_bridge

            install_stdlib_bridge()
        except Exception as exc:
            logger.warning('重装 stdlib 日志桥失败（不影响使用）：{}', exc)
    else:
        url = current_url()
        logger.error('界面服务 {}s 内未就绪，仍尝试打开窗口：{}', SERVER_WAIT_S, url)
        warnings.append(f'界面服务 {SERVER_WAIT_S:.0f} 秒内没起来。\n日志在：{log_path}')

    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    from app.core.db.connection import close_thread_connection

    # QtWebEngine 的缓存 / 存储必须落在可写目录：默认位置在受限环境下写不进去。
    cache_dir = paths.data_dir() / 'qtwebengine'
    (cache_dir / 'cache').mkdir(parents=True, exist_ok=True)
    (cache_dir / 'storage').mkdir(parents=True, exist_ok=True)

    application = QApplication(sys.argv)
    # T2.2 屏幕/DPI 快照：补上技术方案 §10.3 的落空项。屏幕信息必须等 QApplication 才能拿到
    # （启动快照在 bootstrap/ 里、早于 Qt 初始化）。无头/受限会话 primaryScreen 可能为 None。
    screen = application.primaryScreen()
    if screen is None:
        logger.warning("无可用主屏幕，跳过显示环境快照")
    else:
        logger.info(
            "显示环境：屏幕 {}×{}；逻辑 DPI {}；缩放 {:.0%}",
            screen.size().width(), screen.size().height(),
            screen.logicalDotsPerInch(), screen.devicePixelRatio(),
        )
    profile = QWebEngineProfile.defaultProfile()
    profile.setCachePath(str(cache_dir / 'cache'))
    profile.setPersistentStoragePath(str(cache_dir / 'storage'))

    view = QWebEngineView()
    view.setWindowTitle('helper')
    # 前端报错也要进日志（见 _logging_page_class 的说明）；页面必须用同一个 profile，
    # 否则前面设好的缓存/存储路径就白设了。**同时把应用 origin 传进去做外链闸**。
    view.setPage(_logging_page_class(current_url())(profile, view))
    # 万一还有漏网导航（外链闸之外的情况），Alt+Left 能退回应用。
    back_shortcut = QShortcut(QKeySequence('Alt+Left'), view)
    back_shortcut.activated.connect(view.back)

    def on_render_process_gone(status: object, exit_code: int) -> None:
        """渲染进程结束：这是"白板"的一种真成因，必须明确留痕（§10.1「渲染」层）。

        以前只能从"重载窗口 ×3 之后仍空白"反推，现在日志会直接说是渲染进程没了 + 原因码。
        """
        logger.error('渲染进程结束（status={}，exit_code={}），窗口可能变成白板', status, exit_code)

    view.page().renderProcessTerminated.connect(on_render_process_gone)

    # 窗口位置：**每次都在主屏可用区域正中**（不记住上次几何）。外接显示器变化后
    # 旧坐标可能落在屏幕外，固定居中就不会出现"打开却看不见窗口"。
    width, height = _place_window(view, screen, WINDOW_SIZE)
    logger.info('窗口位置：居中于主屏，{}×{}', width, height)

    # 右栏宽度按**真实窗口宽度**算：屏幕小的机器上窗口会被收敛，写死 1440 会挤掉中栏。
    set_window_width(view.width())

    def on_quit() -> None:
        _QUIT.set()
        exit_notes: list[str] = []
        # 0) **先停界面 HTTP 服务**：浏览器（QtWebEngine）关窗时可能还有 keep-alive 与
        #    自动刷新定时器；若先停 Qdrant/worker，这些请求会打到已关闭的后端，
        #    表现为连接重置或 500。先让服务端优雅退出，再停后台进程。
        try:
            from nicegui import app as nicegui_app

            nicegui_app.shutdown()
        except Exception as exc:
            logger.debug('请求停止界面服务失败（可能已在退出）：{}', exc)
        with contextlib.suppress(Exception):
            _server.join(timeout=3.0)
        # **Qdrant/worker 的生命周期与主程序一致**：再停受管服务（worker 收尾 → Qdrant 收尾），
        # 主程序退出后不留下独立服务进程。`stop()` 会**阻塞等每个子进程真正退出**，
        # 返回 ``{name: 状态}``，据此判断是否有子进程没被正常关掉。
        try:
            stop_report = services_service.stop()
        except Exception as exc:
            logger.opt(exception=exc).warning('停止本机服务失败：{}', exc)
            exit_notes.append(f'停止本机服务时出错：{type(exc).__name__}: {exc}')
        else:
            stuck = [f'  - {name}（{state}）' for name, state in stop_report.items()
                     if state == 'still-running']
            if stuck:
                logger.error('以下子进程停止后仍未退出，可能留下孤儿进程：\n{}', '\n'.join(stuck))
                exit_notes.append(
                    '部分后台进程没能正常退出（可能残留孤儿进程）：\n' + '\n'.join(stuck) +
                    '\n请到任务管理器手动结束，或在下次启动后检查。'
                )
        # 同进程模式下让在跑的任务收尾：还没跑完的放回队列（**不算失败**），下次打开接着做
        try:
            tasks_app.stop(grace_s=3.0)
        except Exception as exc:
            logger.warning('任务系统停止失败：{}', exc)
        # **显式关掉 Qdrant client**：不关的话要等解释器退出时它的 ``__del__`` 才跑，
        # 那一刻 ``sys.meta_path`` 已被清空 → 打出一屏
        # ``ImportError: sys.meta_path is None, Python is likely shutting down``。
        # 用 ``shutdown()`` 而不是 ``close()``：它把"最终关闭"闸置真——退出过程中界面
        # 定时器还会跳一次、再去查片段，`client()` 会直接报"正在退出"而不是**重新打开**连接
        # （实测日志：关掉后 600ms 又被打开，那个对象活到解释器卸载才报错）。
        try:
            from app.core.jobs.runner import get_runner
            from app.core.vector import qdrant_store

            runner = get_runner()
            if runner is None or runner.running_count == 0:
                qdrant_store.shutdown()
            else:
                logger.warning('还有 {} 个任务在跑，跳过关闭向量库（交给进程退出）', runner.running_count)
        except Exception as exc:
            logger.warning('关闭向量库失败：{}', exc)
        # **先合并 WAL，再写"干净退出"标记**：退出的顺序反了就可能在库还没合并完时
        # 就宣称"干净退出"，下次启动跳过体检。checkpoint 失败要明确告警——它意味着
        # "只拷 helper.db"的备份不完整（WAL 里还有最近的写入）。
        try:
            close_thread_connection(checkpoint_first=True)
        except Exception as exc:
            logger.warning('退出时关闭数据库连接失败：{}', exc)
        # T3.2 标记"本次干净退出"：下次启动若读不到就做 DB 体检
        try:
            from app.core.db import health as db_health

            db_health.mark_clean_exit()
        except Exception as exc:
            logger.warning('干净退出标记写失败：{}', exc)
        # 收尾行（幂等；atexit 也会来叫它，见 _log_run_summary）
        _log_run_summary(run_started_monotonic, run_started_wall)
        # 退出过程中有异常（子进程没被正常关掉 / 某个收尾步骤失败）就要让用户看见：
        # 关窗瞬间的静默退出不会留下任何让用户警觉的东西，下次讲不清是什么没关干净。
        # 注意：在 aboutToQuit 里弹 modal 是安全的（事件循环仍活着，直到对话框关闭）。
        if exit_notes:
            try:
                _show_box('warning', 'helper 退出提示', '\n\n'.join(exit_notes))
            except Exception as exc:
                logger.warning('退出提示未能弹出：{}', exc)

    application.aboutToQuit.connect(on_quit)

    def flush_warnings() -> None:
        """把攒下的提示弹给用户（一次弹完，别弹一串）。"""
        if not warnings:
            return
        _show_box('warning', 'helper', '\n\n'.join(warnings))
        warnings.clear()

    attempts = {'n': 0}

    def ask_content() -> None:
        """问浏览器：我们的根节点渲染出来了吗（只判断 loadFinished 不够，见模块说明第 3 条）。"""
        view.page().runJavaScript("!!document.querySelector('.hp-sidebar')", on_content)

    def on_content(found: object) -> None:
        if found:
            logger.info('界面已渲染（根节点到位）')
            return
        reload_view('页面到了但内容还没渲染')

    def reload_view(why: str) -> None:
        attempts['n'] += 1
        if attempts['n'] > MAX_RELOAD:
            logger.error('界面仍是空白（{}），已重试 {} 次；服务地址：{}', why, MAX_RELOAD, url)
            # 落一份"用户找得到"的记录：白板的现场在原生输出里，光看 helper.log 看不出所以然
            target = _write_startup_error(
                '界面没有渲染出来',
                f'重试 {MAX_RELOAD} 次仍是空白：{why}\n服务地址：{url}',
            )
            warnings.append(
                f'界面没能显示出来（{why}）。\n服务地址：{url}\n'
                f'诊断记录：{target or log_path}'
            )
            flush_warnings()
            return
        logger.warning('重载窗口（{}，第 {} 次）', why, attempts['n'])
        QTimer.singleShot(RELOAD_DELAY_MS, view.reload)

    def on_loaded(ok: bool) -> None:
        if not ok:
            reload_view('加载失败')
            return
        logger.info('页面已加载：{}', url)
        QTimer.singleShot(1500, ask_content)

    view.loadFinished.connect(on_loaded)
    view.load(QUrl(url))
    view.show()
    QTimer.singleShot(1200, flush_warnings)
    return application.exec()


if __name__ == '__main__':
    raise SystemExit(main())
