"""界面 DOM 断言：在**真实的 QtWebEngine** 里把页面渲染出来，取 HTML 检查关键结构。

**为什么需要它**：NiceGUI 的 `user` 装置（`tests/ui/`）是在进程内模拟用户——没有浏览器、
没有 markdown-it、没有 Vue 渲染，所以"表格到底有没有变成 `<table>`""代码块容器在不在"
这类事只能在真引擎里验。这一层曾经抓出过真问题（代码块被浏览器的 HTML 消毒丢掉 `<pre>`、
会话列表从不画初始状态）。

用法（**在仓库根执行**）：

    .venv\\Scripts\\python.exe -u scripts\\check_ui.py

退出码：全部通过是 0，有失败是 1（可以直接进 CI）。
前提：受限沙箱下跑不了——QtWebEngine 的渲染进程间通信要建命名管道，受限模式会拒绝。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: QtWebEngine 的启动参数。**这三项是实测出来的**（技术方案 §4.1）：少了它们窗口就是一片白板。
#: `--log-level=3` 与 `app/__main__.py` 保持一致：软件渲染下 Chromium 的 GPU 初始化失败是**预期**的，
#: 压到只留 FATAL，免得断言输出里全是噪音（FATAL 仍会打印）。
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --no-sandbox --disable-dev-shm-usage --log-level=3",
)
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

#: 界面服务只监听回环
HOST = "127.0.0.1"

#: 渲染缓存放仓库内（受限环境里系统 TEMP 写不进去）
CACHE = ROOT / ".tmp" / "qtwebengine"

#: 等界面服务真的返回 200 的上限
WAIT_HTTP_S = 25.0

#: 页面加载完再等一会儿：首屏示例是异步渲染的（过程行动画 0.12s + Markdown 渲染）
RENDER_WAIT_MS = 4000

#: 断言清单：``名字 → 判定``（判定拿渲染后的 HTML 文本）。
#: 每条都对应一个真实踩过的坑，所以别随手加删。
CHECKS: dict[str, Callable[[str], bool]] = {
    "设计 token 已注入": lambda t: "--hp-bg-base" in t,
    "左栏 .hp-sidebar": lambda t: "hp-sidebar" in t,
    "顶部栏 .hp-topbar": lambda t: "hp-topbar" in t,
    "界面用「聊天」这个词": lambda t: "聊天" in t,
    "界面里不再出现「问答」": lambda t: "问答" not in t,
    "输入盒 .hp-composer-box": lambda t: "hp-composer-box" in t,
    "左栏「设置」入口": lambda t: "设置" in t,
    "左栏「新建对话」": lambda t: "新建对话" in t,
    "左栏会话列表项": lambda t: 'hp-conv-row' in t,
    "示例会话在列表里": lambda t: "示例对话" in t,
    "左栏顶部「返回聊天」": lambda t: "返回聊天" in t,
    "设置分区（通用 + 模型配置）": lambda t: all(s in t for s in ("通用", "模型配置")),
    "没落地的分区不摆出来": lambda t: not any(s in t for s in ("词库", "追踪")),
    "知识库有导入控件": lambda t: "q-uploader" in t,
    "模型配置两个测试连接": lambda t: t.count("测试连接") >= 2,
    "知识库概览四格": lambda t: all(s in t for s in ("份资料", "个片段", "副本占用", "最后更新")),
    "知识库有全选本页": lambda t: "全选本页" in t,
    "任务页有正在跑面板": lambda t: "正在跑（" in t,
    "任务页有队列控制": lambda t: all(s in t for s in ("暂停队列", "重试全部失败", "清理已完成")),
    "任务页有清理存储入口": lambda t: "清理存储" in t,
    "任务页有全选本页": lambda t: "全选本页" in t,
    "列表卡骨架（工具栏 + 页脚）": lambda t: "hp-toolbar" in t and "hp-list-footer" in t,
    "自动刷新三件套": lambda t: all(s in t for s in ("自动刷新", "立即刷新")),
    "分页页脚有每页条数": lambda t: "每页 20 条" in t and "hp-pager" in t,
    "高级参数不上界面": lambda t: "温度" not in t and "最大输出" not in t,
    "写明配置进数据库": lambda t: "llm_slots" in t,
    "模拟数据已明确标注": lambda t: "示例数据" in t,
    # 下面几条是"格式真的渲染出来了"的硬证据——用户模拟里看不到这一层
    "Markdown 表格 → <table>": lambda t: "<table" in t,
    # 浏览器内置的 HTML 消毒会丢掉 <pre>，pygments 的 .codehilite 容器才是留下的那个
    "代码块容器 .codehilite": lambda t: "codehilite" in t,
    "行内代码 → <code>": lambda t: "<code" in t,
    "引用角标 .hp-cite": lambda t: "hp-cite" in t,
    "过程行 .hp-process": lambda t: "hp-process" in t,
    "过程行是可收起元素": lambda t: "q-expansion-item" in t,
    "答案容器 .hp-markdown": lambda t: "hp-markdown" in t,
    "设置弹框已移除": lambda t: "hp-settings-card" not in t,
}

#: 第二段检查：**量可见高度**。
#: 光"在 HTML 里"不算数——我们自己踩过：元素在、但被同级元素挤成 0 高，用户看到的是白板
#: （设置页那个滚动区就是因为建错了父节点被挤没的）。这里进设置、切到模型配置，各量一次。
MEASURE_SCRIPT = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const height = (sel) => {
    const el = document.querySelector(sel);
    return el ? Math.round(el.getBoundingClientRect().height) : -1;
  };
  const button = (text) => [...document.querySelectorAll('button')]
      .find((b) => (b.innerText || '').includes(text));
  const out = {};
  try {
    out['聊天 · 输入盒'] = height('.hp-composer-box');
    out['聊天 · 一轮对话'] = height('.hp-turn');

    const settings = button('设置');
    if (settings) settings.click();
    await sleep(800);
    out['设置 · 设置容器'] = height('.hp-settings-box');
    out['设置 · 内容容器'] = height('.hp-settings-body');
    out['设置 · 滚动区'] = height('.hp-settings-scroll');
    out['设置 · 分区标题'] = height('.hp-page-title');

    const section = [...document.querySelectorAll('button')]
        .find((b) => (b.innerText || '').includes('模型配置'));
    if (section) section.click();
    await sleep(800);
    out['设置 · 模型槽位卡'] = height('.hp-slot');

    const knowledge = [...document.querySelectorAll('button')]
        .find((b) => (b.innerText || '').includes('知识库'));
    if (knowledge) knowledge.click();
    await sleep(800);
    out['设置 · 知识库导入控件'] = height('.hp-upload');
    out['设置 · 知识库页脚'] = height('.hp-kb-list .hp-list-footer');

    const tasks = [...document.querySelectorAll('button')]
        .find((b) => (b.innerText || '').includes('任务'));
    if (tasks) tasks.click();
    await sleep(800);
    out['设置 · 任务页活面板'] = height('.hp-tasks-running');
    out['设置 · 任务页工具栏'] = height('.hp-tasks-list .hp-toolbar');
    out['设置 · 任务页页脚'] = height('.hp-tasks-list .hp-list-footer');
  } catch (error) {
    out['X_脚本异常'] = String(error);
  }
  window.__measure = JSON.stringify(out);
})()
"""


def free_port() -> int:
    """向系统要一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, timeout_s: float = WAIT_HTTP_S) -> bool:
    """等界面服务真的返回 200（只等 TCP 端口不够：那时页面还是空的）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.15)
    return False


def main() -> int:
    """跑一遍断言。返回进程退出码（0 = 全过）。"""
    from nicegui import ui

    from app.bootstrap import ensure_ready
    from app.nicegui_patch import skip_unused_process_pool
    from app.ui.shell import build_shell

    ensure_ready()
    skip_unused_process_pool()  # 去掉那个用不到的进程池（受限环境下它会因命名管道直接失败）

    @ui.page('/')
    def index() -> None:
        build_shell()

    port = free_port()
    url = f'http://{HOST}:{port}/'
    threading.Thread(
        target=lambda: ui.run(host=HOST, port=port, show=False, reload=False, native=False),
        name='nicegui-server',
        daemon=True,
    ).start()
    if not wait_http(url):
        print(f'界面服务 {WAIT_HTTP_S:.0f}s 内没起来：{url}', file=sys.stderr)
        return 2
    print('界面服务就绪：', url, flush=True)

    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    CACHE.mkdir(parents=True, exist_ok=True)
    application = QApplication(sys.argv)
    profile = QWebEngineProfile.defaultProfile()
    profile.setCachePath(str(CACHE / 'cache'))
    profile.setPersistentStoragePath(str(CACHE / 'storage'))

    view = QWebEngineView()
    # **必须让控件"有视口"**：不 show() 时 window.innerHeight 是 0，`100vh` 会算成 0，
    # 于是所有靠确定高度布局的元素都量成 0——量高度那一组断言就全是假的。
    # WA_DontShowOnScreen：按尺寸布局但不真的弹窗。
    view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    view.resize(1440, 900)
    view.show()
    failed: list[str] = []
    checked = 0

    def on_measure(raw: object) -> None:
        nonlocal checked
        try:
            data = json.loads(str(raw or "{}"))
        except ValueError:
            data = {"X_量高度脚本没返回": -1}
        print('\n=== 可见高度（元素在 HTML 里不算数，要看真的有没有高度）===', flush=True)
        for name, value in data.items():
            checked += 1
            ok = isinstance(value, int) and value > 0
            print(('PASS  ' if ok else 'FAIL  ') + f'{name}：{value} px', flush=True)
            if not ok:
                failed.append(f'{name}（可见高度 {value}）')
        print(f'\n共 {checked} 项，{checked - len(failed)} 项通过', flush=True)
        application.quit()

    def fetch_measure() -> None:
        view.page().runJavaScript("window.__measure || '{}'", on_measure)

    def on_html(html: str) -> None:
        nonlocal checked
        text = html or ''
        print('HTML 长度:', len(text), flush=True)
        for name, check in CHECKS.items():
            checked += 1
            ok = check(text)
            print(('PASS  ' if ok else 'FAIL  ') + name, flush=True)
            if not ok:
                failed.append(name)
        # 接着量可见高度：只查 HTML 会漏掉"元素在、但被挤成 0 高"这种白板问题
        view.page().runJavaScript(MEASURE_SCRIPT)
        QTimer.singleShot(4500, fetch_measure)

    def probe() -> None:
        view.page().toHtml(on_html)

    def on_loaded(ok: bool) -> None:
        print('loadFinished:', ok, '| url:', view.url().toString(), flush=True)
        QTimer.singleShot(RENDER_WAIT_MS, probe)

    view.loadFinished.connect(on_loaded)
    view.load(QUrl(url))
    QTimer.singleShot(45000, application.quit)  # 看门狗：无论如何 45 秒退出，别挂住
    application.exec()
    if failed:
        print('\n失败项：', '；'.join(failed), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
