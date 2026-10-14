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

#: 界面服务只监听回环（技术方案 §5.1 红线 4）。NiceGUI 默认是 0.0.0.0，所以这一行不能省。
HOST = "127.0.0.1"

#: 等界面服务就绪的上限（秒）；超时也照样把窗口开出来，但会弹提示告诉用户去看日志。
SERVER_WAIT_S = 20.0

#: 服务就绪后再等一会儿让 uvicorn 完全稳下来（避免"端口通了但请求还失败"的窗口期）。
SETTLE_S = 0.4

#: 加载失败 / 内容没渲染时最多重载几次。
MAX_RELOAD = 3
RELOAD_DELAY_MS = 800

#: 启动失败的记录文件名（先写 logs/，写不进去再写安装目录）。
STARTUP_ERROR_FILE = "startup-error.log"


def _free_port() -> int:
    """向系统要一个空闲端口（桌面应用不该因为端口被占就起不来）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, timeout_s: float = SERVER_WAIT_S) -> bool:
    """等界面服务真的返回 HTTP 200（不是只等端口）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.15)
    return False


def _write_startup_error(title: str, detail: str) -> Path | None:
    """把启动失败写进一个**用户找得到**的文件，返回实际写入的路径。

    为什么不只靠日志：日志系统本身可能正是失败的那一环（目录不可写、坏配置隔离发生在
    建 sink 之前），所以这里不依赖任何前提，直接写文件；两个位置都写不进去才放弃。
    """
    from app.core import paths

    lines = [f"helper 启动失败：{title}", ""]
    lines += [f"{name}：{value}" for name, value in paths.describe().items()]
    lines += ["", detail]
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

    port = _free_port()
    url = f'http://{HOST}:{port}/'
    warnings: list[str] = []

    # 界面服务跑在后台线程：主线程必须留给 Qt 的事件循环（窗口只能由主线程创建）。
    _server = threading.Thread(
        target=lambda: ui.run(
            host=HOST,
            port=port,
            title='helper',
            favicon='🧩',
            show=False,     # 明确：不许打开浏览器
            reload=False,   # 桌面窗口绝不能热重载
            native=False,   # 窗口由下面的 QtWebEngine 提供
            dark=False,     # 只做浅色（DSH 浅色色板）
        ),
        name='nicegui-server',
        daemon=True,
    )
    _server.start()

    log_path = paths.logs_dir() / 'helper.log'
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
            warnings.append(f'界面没能显示出来（{why}）。\n服务地址：{url}\n日志在：{log_path}')
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
