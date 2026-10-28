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
from collections.abc import Callable
from pathlib import Path

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

#: 页面加载完再等一会儿：首屏是异步渲染的（种子会话 + Markdown 渲染）
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
    "设置分区（通用 + 模型配置 + 检索追踪）": lambda t: all(s in t for s in ("通用", "模型配置", "检索追踪")),
    "旧分区不摆出来": lambda t: "词库" not in t,
    "知识库有导入控件": lambda t: "q-uploader" in t,
    "模型配置两个测试连接": lambda t: t.count("测试连接") >= 2,
    "知识库概览四格": lambda t: all(s in t for s in ("份资料", "个片段", "副本占用", "向量占用")),
    "知识库有全选本页": lambda t: "全选本页" in t,
    "任务页有正在跑面板": lambda t: "正在跑（" in t,
    "任务页有队列控制": lambda t: all(s in t for s in ("暂停队列", "重试全部失败", "清理已完成")),
    "任务页有清理存储入口": lambda t: "清理存储" in t,
    "任务页有全选本页": lambda t: "全选本页" in t,
    "列表卡骨架（工具栏 + 页脚）": lambda t: "hp-toolbar" in t and "hp-list-footer" in t,
    "自动刷新三件套": lambda t: all(s in t for s in ("自动刷新", "立即刷新")),
    # 空库时列表是空态、分页控件不渲染；分页的"真的有高度"交给下面量高度（会点开「资料列表」）
    "知识库三页签": lambda t: all(s in t for s in ("总览", "资料列表", "导入")),
    "高级参数不上界面": lambda t: "温度" not in t and "最大输出" not in t,
    "写明配置进数据库": lambda t: "llm_slots" in t,
    "模拟数据已明确标注": lambda t: "示例数据" in t,
    # 下面是聊天区这一轮的改动（等待反馈 + 底栏统计）：
    # 等待行有动画元素（CSS 注入即出现，运行时也一直存在）
    "等待行动画（running 行 + 跳动点）": lambda t: "hp-running-row" in t and "hp-dots" in t,
    "等待行秒表样式": lambda t: "hp-running-clock" in t,
    "聊天底栏统计 dock": lambda t: "hp-composer-stats" in t,
    "上下文占用条": lambda t: "hp-context-bar" in t,
    "统计数字等宽": lambda t: "hp-stat-pill" in t,
    # 工作步骤（工具/命令行）与推理预览：种子会话里的一轮会真的画出这些行
    "工作步骤行（工具/命令）": lambda t: "hp-step-title" in t,
    "推理预览块": lambda t: "hp-reasoning-title" in t,
    # 资料区默认收起：只露一行"资料 · N 段"，点开才列出出处
    "资料区折叠条（默认收起）": lambda t: "资料 · 1 段" in t,
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
    out['聊天 · 底栏统计'] = height('.hp-composer-stats');

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
    await sleep(1000);
    // 知识库页签是 Quasar 的 q-tab（div，不是 button）：按文字找 tab 再点
    const tab = (text) => [...document.querySelectorAll('.q-tab, [role="tab"]')]
        .find((t) => (t.innerText || '').includes(text));
    const listTab = tab('资料列表');
    if (listTab) listTab.click();
    await sleep(1000);
    out['设置 · 知识库列表卡'] = height('.hp-kb-list');
    out['设置 · 知识库工具栏'] = height('.hp-kb-list .hp-toolbar');
    out['设置 · 知识库列表页脚'] = height('.hp-kb-list .hp-list-footer');
    out['设置 · 知识库分页节点'] = document.querySelectorAll('.hp-kb-list .hp-pager').length;
    const importTab = tab('导入');
    if (importTab) importTab.click();
    await sleep(1000);
    out['设置 · 知识库导入控件节点'] = document.querySelectorAll('.q-uploader').length;

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


def seed_sample_conversation() -> None:
    """渲染检查用：给聊天页种一个带完整一轮的示例会话。

    真实应用里聊天页自己种"新对话"（空会话，见 `_chat_retrieval.ensure_seeded`）；
    这里种一个**有内容**的会话，让"表格/代码块/引用/过程行真的渲染出来了"
    那组断言有东西可查——这正是本脚本要验的那一层（用户模拟看不到
    markdown-it 的产物）。
    """
    from app.ui.state import conversations as store
    from app.ui.state.chat_model import Entry, Fragment, Process, Step, Turn

    if store.conversations():
        return
    store.new(
        title="示例对话",
        note="示例数据（界面展示用，不是真实检索结果）",
    )
    store.append(
        Entry(
            question="违约金怎么算？",
            turn=Turn(
                process=Process(
                    running="正在检索本地资料…",
                    done="检索本地资料 · hybrid · 命中 1 条",
                    steps=[
                        Step(group="prepare", title="理解问题", detail="查询词：违约金怎么算？"),
                        Step(group="search", title="语义检索", detail="命中 1 条"),
                        Step(
                            group="filter",
                            title="回填父块",
                            detail="取回 1 个父块 → 1 条证据\n#1 1.00 · 合同.md · 第 5 段",
                        ),
                    ],
                    reasoning="- 查询词：违约金怎么算？\n- 检索模式：hybrid（通道：semantic + keyword）",
                    duration_ms=12,
                ),
                answer=(
                    "#### 合同.md · 第 5 段\n\n"
                    "违约金按日万分之三计算。\n\n"
                    "| 条款 | 说明 |\n| --- | --- |\n| 违约金 | 日万分之三 |\n\n"
                    "```python\nprint('示例')\n```\n\n行内代码 `示例`。"
                ),
                fragments=[
                    Fragment(
                        index=1,
                        source_name="合同.md",
                        origin="第 5 段",
                        score=1.0,
                        text="违约金按日万分之三计算。",
                    ),
                ],
            ),
        )
    )


def wait_http(url: str, timeout_s: float = WAIT_HTTP_S) -> bool:
    """等界面服务真的返回 200（只等 TCP 端口不够：那时页面还是空的）。"""
    deadline = time.monotonic() + timeout_s
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5.0) as response:
                if response.status == 200:
                    return True
                last = RuntimeError(f"HTTP {response.status}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            time.sleep(0.15)
    if last is not None:
        print(f"wait_http 最后错误：{type(last).__name__}: {last}", file=sys.stderr)
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
        seed_sample_conversation()
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
        empty_kb = data.get("设置 · 知识库分页节点") == 0
        for name, value in data.items():
            checked += 1
            # 空库/空任务列表时页脚可以自然塌成 0（有内容时必须 >0）
            ok = isinstance(value, int) and (
                value > 0
                or (empty_kb and name == "设置 · 知识库列表页脚" and value == 0)
                or ("任务页页脚" in name and value == 0)
                or ("知识库分页节点" in name and value == 0)
            )
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
        QTimer.singleShot(9000, fetch_measure)

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
