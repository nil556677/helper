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

import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

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

#: 启动失败的记录文件名（先写 logs/，写不进去再写安装目录）。
STARTUP_ERROR_FILE = "startup-error.log"


def _install_qt_message_handler() -> None:
    """把 Qt 自己的 warning/critical 送进日志（技术方案 §10.1 的"渲染"层）。

    为什么放在这里而不是 ``core/logging``：core 不许 import 界面库（红线 1），
    而 ``qInstallMessageHandler`` 必须 import ``PySide6.QtCore``。
    """
    from loguru import logger as qt_logger
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    levels = {
        QtMsgType.QtDebugMsg: "DEBUG",
        QtMsgType.QtInfoMsg: "INFO",
        QtMsgType.QtWarningMsg: "WARNING",
        QtMsgType.QtCriticalMsg: "ERROR",
        QtMsgType.QtFatalMsg: "CRITICAL",
    }

    def handler(mode: QtMsgType, context: object, message: str) -> None:  # noqa: ARG001
        level = levels.get(mode, "INFO")
        qt_logger.opt(depth=1).log(level, "Qt: {}", message)

    qInstallMessageHandler(handler)


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

    console = capture_native_output()          # ① 原生输出（fd 1+2）→ logs/console-<日期>.log
    # 捕获会把 sys.stdout/stderr 换成新流（绑定到刚接好的 fd），所以日志的**控制台 sink 要重建**：
    # 否则它还握着旧流——旧流编码是 GBK（console 文件里中文变乱码），且可能已经是坏句柄。
    init_logging(access.load().log_level)
    install_stdlib_bridge()                    # ② stdlib logging（NiceGUI/uvicorn/asyncio/PySide6）
    install_asyncio_handler()                  # ③ asyncio 任务异常（界面回调）
    _install_qt_message_handler()              # ④ Qt 自己的消息
    if console is not None:
        entry_logger.info("原生输出已捕获：{}", console)


def _listen_host() -> str:
    """界面服务的监听地址（来自 ``config.json``）。

    默认回环；配置里改成非回环地址时**打一条 WARNING**：界面没有任何认证，绑到局域网
    等于谁都能打开它看资料。不拒绝（用户可能确实要这么干），但要留下痕迹。
    """
    from loguru import logger

    from app.core.config import access
    from app.core.config.schema import is_loopback

    host = access.load().host
    if not is_loopback(host):
        logger.warning(
            "监听地址不是回环（{}）：界面没有认证，同一网络里任何人都能打开它。"
            "如果不是本意，把 config.json 的 host 改回 127.0.0.1",
            host,
        )
    return host


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


def _wait_http(url: str, timeout_s: float = SERVER_WAIT_S, *, report_every_s: float = 5.0) -> bool:
    """等界面服务真的返回 HTTP 200（不是只等端口）。"""
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    next_report = report_every_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.15)
        waited = time.monotonic() - started
        if waited >= next_report:
            next_report += report_every_s
            logger.warning('界面服务还没就绪（已等 {:.0f}s）：{}', waited, url)
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
    """启动桌面窗口（阻塞到窗口关闭）。"""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from loguru import logger

    from app.bootstrap import ensure_ready
    from app.core import paths

    # 启动前提不满足（目录建不出来 / 库损坏 / 迁移缺脚本）时**必须让用户看见**：
    # 双击启动没有常驻控制台，以前这种情况就是"闪一下、什么都没有"。
    try:
        ensure_ready()
    except Exception as exc:  # noqa: BLE001 - 启动前提失败：转成用户可读的提示再退出
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
    from app.services import preferences
    from app.ui.shell import WINDOW_SIZE, build_shell, set_window_width

    # 去掉 NiceGUI 那个我们用不到的进程池（它在受限环境下会因命名管道直接失败，见模块说明）
    skip_unused_process_pool()

    # 任务系统：先做崩溃恢复（把上次中断的任务收回队列），再起调度线程。
    # 放在界面之前：上次没导完的资料，用户打开窗口时它已经在跑了。
    tasks_app.start()

    @ui.page('/')
    def index() -> None:
        build_shell()

    _install_log_channels()

    host = _listen_host()
    port = _free_port(host)
    url = f'http://{host}:{port}/'
    warnings: list[str] = []

    def serve() -> None:
        """界面服务线程的入口。

        **为什么要包一层函数而不是直接 lambda**：这一层以前是"哑"的——服务起不来时日志里
        只有 20 秒后的一句"未就绪"，看不出是抛了异常、还是卡住了、还是退出了（实测踩过：
        无控制台启动时偶发卡住，没有任何证据）。现在它把"开始 / 正常退出 / 异常退出"都记下来。
        """
        logger.info('界面服务线程启动：{}:{}', host, port)
        try:
            ui.run(
                host=host,
                port=port,
                title='helper',
                favicon='🧩',
                show=False,     # 明确：不许打开浏览器
                reload=False,   # 桌面窗口绝不能热重载
                native=False,   # 窗口由下面的 QtWebEngine 提供
                dark=False,     # 只做浅色（DSH 浅色色板）
            )
        except SystemExit as exc:
            # uvicorn 启动失败会 sys.exit(3)，而且**失败原因被它自己的 logging 配置吞掉**
            # （实测：uvicorn.error 的 handler 被 dictConfig 清空）。所以这里自己补上：
            # 立刻再绑一次端口，把"是不是端口问题"直接写进日志 + startup-error.log。
            logger.opt(exception=exc).critical('界面服务启动失败（SystemExit {}）：{}:{}', exc.code, host, port)
            logger.error('端口自检：{}', _probe_bind(host, port))
            _write_startup_error(
                '界面服务没能起来',
                f'uvicorn 以 SystemExit({exc.code}) 退出；地址 {host}:{port}\n'
                f'端口自检：{_probe_bind(host, port)}',
            )
            return
        except BaseException as exc:  # noqa: BLE001 - 服务线程死掉必须留证据
            logger.opt(exception=exc).critical('界面服务线程异常退出：{}', exc)
            _write_startup_error('界面服务线程异常退出', f'{type(exc).__name__}: {exc}\n地址 {host}:{port}')
            return
        logger.warning('界面服务线程已退出（服务不再可用）')

    # 界面服务跑在后台线程：主线程必须留给 Qt 的事件循环（窗口只能由主线程创建）。
    _server = threading.Thread(target=serve, name='nicegui-server', daemon=True)
    _server.start()

    from app.core.logging import daily_log_file

    log_path = daily_log_file()
    if _wait_http(url):
        logger.info('界面服务就绪：{}', url)
        time.sleep(SETTLE_S)
    else:
        logger.error('界面服务 {}s 内未就绪，仍尝试打开窗口：{}', SERVER_WAIT_S, url)
        warnings.append(f'界面服务 {SERVER_WAIT_S:.0f} 秒内没起来。\n日志在：{log_path}')

    from PySide6.QtCore import QByteArray, QTimer, QUrl
    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    from app.core.db.connection import close_thread_connection

    # QtWebEngine 的缓存 / 存储必须落在可写目录：默认位置在受限环境下写不进去。
    cache_dir = paths.data_dir() / 'qtwebengine'
    (cache_dir / 'cache').mkdir(parents=True, exist_ok=True)
    (cache_dir / 'storage').mkdir(parents=True, exist_ok=True)

    application = QApplication(sys.argv)
    profile = QWebEngineProfile.defaultProfile()
    profile.setCachePath(str(cache_dir / 'cache'))
    profile.setPersistentStoragePath(str(cache_dir / 'storage'))

    view = QWebEngineView()
    view.setWindowTitle('helper')

    def on_render_process_gone(status: object, exit_code: int) -> None:
        """渲染进程结束：这是"白板"的一种真成因，必须明确留痕（§10.1「渲染」层）。

        以前只能从"重载窗口 ×3 之后仍空白"反推，现在日志会直接说是渲染进程没了 + 原因码。
        """
        logger.error('渲染进程结束（status={}，exit_code={}），窗口可能变成白板', status, exit_code)

    view.page().renderProcessTerminated.connect(on_render_process_gone)

    # 窗口几何：上次调过就照原样恢复（大小 + 位置，含最大化状态）；没存过 / 存坏了 /
    # 位置已经不在屏幕上（换了显示器）时 restoreGeometry 返回 False，就退回默认尺寸。
    saved_geometry = preferences.load_window_geometry()
    if saved_geometry and view.restoreGeometry(QByteArray(saved_geometry)):
        logger.info('已恢复上次的窗口几何')
    else:
        view.resize(*WINDOW_SIZE)
        logger.info('使用默认窗口尺寸 {}×{}', *WINDOW_SIZE)

    # 右栏宽度按**真实窗口宽度**算：窗口大小是用户上次调过的，写死 1440 会挤掉中栏。
    set_window_width(view.width())

    def remember_geometry() -> None:
        """退出前把窗口几何记下来（下次进来保持一致）。"""
        try:
            preferences.save_window_geometry(bytes(view.saveGeometry()))
            logger.info('已记住窗口几何')
        except Exception as exc:  # noqa: BLE001 - 允许失败：记不住窗口大小不该影响退出
            logger.warning('窗口几何保存失败：{}', exc)

    def on_quit() -> None:
        # 先让在跑的任务收尾：还没跑完的放回队列（**不算失败**），下次打开接着做
        try:
            tasks_app.stop(grace_s=3.0)
        except Exception as exc:  # noqa: BLE001 - 允许失败：任务没收完不该挡住退出
            logger.warning('任务系统停止失败：{}', exc)
        remember_geometry()
        # 关连接前把 WAL 合并回主库：否则"只拷 helper.db"的备份会丢最近的写入（§9）
        close_thread_connection(checkpoint_first=True)

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
