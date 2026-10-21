"""模拟真人走查 UI：真实 QtWebEngine 打开软件，按人的路径点击，收集错误。

走查路径（每步都记录成功/失败与关键元素高度）：
  首屏 → 左栏「设置」→ 通用 → 模型配置 → 知识库（总览/资料列表/导入）
  → 打开一份资料的详情 → 返回列表 → 全选本页 → 取消选择
  → 向量页 → 任务页 → 返回聊天 → 点一个会话

错误收集（三类）：
  1. JS 控制台 warning/error（QWebEnginePage.javaScriptConsoleMessage）
  2. 未捕获异常与未处理的 Promise（window.onerror / unhandledrejection）
  3. Python 端 ERROR 日志（loguru sink）

用法（**必须在不受限会话**，QtWebEngine 要命名管道）：
    .venv\\Scripts\\python.exe -u scripts\\sim_ui_walkthrough.py
退出码：0 = 无错误；1 = 走查步骤失败或有错误记录。
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --no-sandbox --disable-dev-shm-usage --log-level=3",
)
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

HOST = "127.0.0.1"
CACHE = ROOT / ".tmp" / "qtwebengine"
WAIT_HTTP_S = 25.0

#: 错误钩子：装上之后，任何 JS 异常/未处理 rejection 都进 window.__errs
ERROR_HOOKS = r"""
window.__errs = window.__errs || [];
window.__log = window.__log || [];
window.addEventListener('error', (e) => {
  const where = (e.filename || '') + ':' + (e.lineno || 0);
  window.__errs.push('error: ' + ((e.message || e) + ' @ ' + where));
});
window.addEventListener('unhandledrejection', (e) => {
  const r = e.reason || {};
  window.__errs.push('rejection: ' + (r.message || r || 'unknown'));
});
true
"""

#: 走查步骤（模拟人操作）：返回步骤结果数组
WALKTHROUGH = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const height = (sel) => {
    const el = document.querySelector(sel);
    return el ? Math.round(el.getBoundingClientRect().height) : -1;
  };
  const button = (text) => [...document.querySelectorAll('button')]
      .find((b) => (b.innerText || '').includes(text));
  const tab = (text) => [...document.querySelectorAll('.q-tab, [role="tab"]')]
      .find((t) => (t.innerText || '').includes(text));
  const clickText = async (text, label, wait = 700) => {
    const el = button(text) || tab(text);
    if (!el) { window.__log.push({ step: label, ok: false, detail: '找不到按钮：' + text }); return false; }
    el.click();
    await sleep(wait);
    window.__log.push({ step: label, ok: true, detail: '' });
    return true;
  };
  const clickSel = async (sel, label, wait = 600) => {
    const el = document.querySelector(sel);
    if (!el) { window.__log.push({ step: label, ok: false, detail: '找不到元素：' + sel }); return false; }
    el.click();
    await sleep(wait);
    window.__log.push({ step: label, ok: true, detail: '' });
    return true;
  };

  window.__log.push({ step: '首屏', ok: document.querySelector('.hp-sidebar') !== null,
                      detail: '聊天输入盒 ' + height('.hp-composer-box') + 'px' });

  // 左栏「设置」
  await clickText('设置', '打开设置');
  await sleep(600);
  window.__log.push({ step: '设置容器', ok: height('.hp-settings-box') > 0,
                      detail: height('.hp-settings-box') + 'px' });

  // 逐个分区
  for (const name of ['通用', '模型配置', '向量', '任务']) {
    await clickText(name, '分区：' + name);
    await sleep(500);
    window.__log.push({
      step: '分区内容：' + name,
      ok: height('.hp-settings-body') > 0,
      detail: 'body ' + height('.hp-settings-body') + 'px',
    });
  }

  // 知识库三个页签
  await clickText('知识库', '分区：知识库');
  await sleep(800);
  window.__log.push({ step: '知识库概览', ok: height('.hp-stats') > 0,
                      detail: '统计格 ' + height('.hp-stats') + 'px' });
  if (tab('资料列表')) { tab('资料列表').click(); await sleep(900); }
  window.__log.push({ step: '资料列表', ok: document.querySelectorAll('.hp-kb-list').length > 0,
                      detail: '列表卡 ' + height('.hp-kb-list') + 'px' });
  if (tab('导入')) { tab('导入').click(); await sleep(900); }
  window.__log.push({ step: '导入页', ok: document.querySelectorAll('.q-uploader').length > 0,
                      detail: '上传控件节点 ' + document.querySelectorAll('.q-uploader').length });

  // 回到列表 → 打开一份资料详情（点资料名，不是行上的图标按钮）
  if (tab('资料列表')) { tab('资料列表').click(); await sleep(900); }
  const nameEl = [...document.querySelectorAll('div, span, a')].find(
      (el) => el.children.length === 0 && (el.innerText || '').includes('ui-walk'));
  if (nameEl) {
    (nameEl.closest('button') || nameEl).click();
    await sleep(1200);
    const opened = document.body.innerText.includes('文档标题：');
    window.__log.push({ step: '打开资料详情', ok: opened,
                        detail: opened ? '详情卡已渲染' : '未出现详情' });
    // 详情视图用「返回列表」按钮回列表（页签在详情视图里不显示）
    const backBtn = button('返回列表') || tab('资料列表');
    if (backBtn) { backBtn.click(); await sleep(900); }
    window.__log.push({ step: '返回列表', ok: document.querySelectorAll('.hp-kb-list').length > 0,
                        detail: '' });
  } else {
    window.__log.push({ step: '打开资料详情', ok: false, detail: '列表里没有 ui-walk 行' });
  }

  // 全选本页 → 取消选择
  const selectPage = button('全选本页') || button('全选');
  if (selectPage) {
    selectPage.click(); await sleep(600);
    window.__log.push({ step: '全选本页', ok: true, detail: '' });
    const clear = button('取消选择') || button('清除选择');
    if (clear) { clear.click(); await sleep(500); }
  }

  // 返回聊天，点一个会话
  await clickText('返回聊天', '返回聊天');
  await sleep(600);
  const conv = document.querySelector('.hp-conv-row');
  if (conv) { conv.click(); await sleep(700); }
  window.__log.push({ step: '点开会话', ok: conv !== null, detail: '' });

  // 收尾：把设置面板关掉（页面回到干净状态）
  const closeBtn = document.querySelector('.hp-dialog-close, .hp-panel-close');
  if (closeBtn) { closeBtn.click(); await sleep(400); }
  window.__log.push({ step: '关闭面板', ok: true, detail: '' });

  window.__done = true;
  true
})()
"""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, timeout_s: float = WAIT_HTTP_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.15)
    return False


def seed_docs() -> list[int]:
    """灌两份样例资料（走查要有数据可点），返回 doc_id。"""
    import numpy as np

    from app.core.config import store as cfg
    from app.rag.ingest import import_bytes as _import_bytes

    def fake_embed(texts: Sequence[str]) -> np.ndarray:
        size = int(cfg.load()["embed.dims"])
        return np.stack([np.ones(size, dtype=np.float32) / np.sqrt(size) for _ in texts])

    docs = [
        ("# ui-walk 合同\n\n甲方应当在三十日内交付设备。\n\n## 付款\n\n验收合格后付款。\n", "ui-walk-1.md"),
        ("# ui-walk 说明\n\n这是第二份走查样例，用于列表与详情。\n", "ui-walk-2.md"),
    ]
    ids = []
    for body, name in docs:
        result = _import_bytes(body.encode(), name, embed=fake_embed)
        if result.doc_id:
            ids.append(int(result.doc_id))
    return ids


def main() -> int:
    from loguru import logger

    from app.bootstrap import ensure_ready
    from app.nicegui_patch import skip_unused_process_pool
    from app.ui.shell import build_shell

    # Python 端 ERROR 日志收集
    py_errors: list[str] = []

    def sink(message: object) -> None:
        record = getattr(message, "record", None)
        text = str(record["message"]) if record is not None else str(message)
        if text not in py_errors:
            py_errors.append(text)

    ensure_ready()  # 先初始化：它会重建日志 handler，sink 必须加在它之后
    handler = logger.add(sink, level="ERROR", format="{message}")
    seeded = seed_docs()

    try:
        skip_unused_process_pool()

        from nicegui import ui

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
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication

        CACHE.mkdir(parents=True, exist_ok=True)
        application = QApplication(sys.argv)

        console: list[dict] = []

        class ProbePage(QWebEnginePage):
            def javaScriptConsoleMessage(self, level, message, line, source):
                console.append(
                    {"level": int(level), "message": str(message), "line": int(line), "source": str(source)}
                )

        profile = QWebEngineProfile.defaultProfile()
        profile.setCachePath(str(CACHE / 'cache'))
        profile.setPersistentStoragePath(str(CACHE / 'storage'))

        view = QWebEngineView()
        page = ProbePage(profile, view)
        view.setPage(page)
        view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        view.resize(1440, 900)
        view.show()

        report: dict[str, object] = {}

        def on_done(raw: object) -> None:
            report["log"] = str(raw or "[]")
            page.runJavaScript("JSON.stringify(window.__errs || [])", on_errs)

        def on_errs(raw: object) -> None:
            report["js_errors"] = str(raw or "[]")
            application.quit()

        def run_steps() -> None:
            page.runJavaScript("JSON.stringify(window.__log || [])", on_done)

        def on_loaded(ok: bool) -> None:
            print('loadFinished:', ok, '| url:', view.url().toString(), flush=True)
            page.runJavaScript(ERROR_HOOKS)
            QTimer.singleShot(2500, lambda: page.runJavaScript(WALKTHROUGH))

            def poll_done() -> None:
                """轮询 window.__done：走查步骤有固定 sleep，别用固定超时猜。"""
                page.runJavaScript(
                    "!!window.__done",
                    lambda done: run_steps() if done else QTimer.singleShot(400, poll_done),
                )

            QTimer.singleShot(3000, poll_done)

        view.loadFinished.connect(on_loaded)
        view.load(QUrl(url))
        QTimer.singleShot(45000, application.quit)
        application.exec()

        # ---- 打印报告 ----
        steps = json.loads(str(report.get("log") or "[]"))
        js_errors = json.loads(str(report.get("js_errors") or "[]"))
        print('\n=== 走查步骤 ===', flush=True)
        failed_steps = 0
        for item in steps:
            ok = bool(item.get("ok"))
            failed_steps += 0 if ok else 1
            detail = f"（{item['detail']}）" if item.get("detail") else ""
            print(('PASS  ' if ok else 'FAIL  ') + str(item.get("step")) + detail, flush=True)

        bad_console = [c for c in console if c["level"] >= 2]  # 2=warning, 3=error
        print('\n=== JS 控制台（warning/error）===', flush=True)
        if bad_console:
            for item in bad_console[:30]:
                print(f"[{item['level']}] {item['message']}  ({item['source']}:{item['line']})", flush=True)
        else:
            print('（无）', flush=True)

        print('\n=== 未捕获 JS 异常 ===', flush=True)
        print('\n'.join(js_errors) if js_errors else '（无）', flush=True)

        print('\n=== Python ERROR 日志 ===', flush=True)
        print('\n'.join(py_errors) if py_errors else '（无）', flush=True)

        total_errors = failed_steps + len(bad_console) + len(js_errors) + len(py_errors)
        print(f'\n共 {len(steps)} 步，失败 {failed_steps} 步；'
              f'控制台告警/错误 {len(bad_console)} 条；JS 异常 {len(js_errors)} 条；'
              f'Python ERROR {len(py_errors)} 条', flush=True)
        return 1 if total_errors else 0
    finally:
        # 清理走查样例，别把测试数据留在真实库里（放最前，异常也不影响）
        try:
            from app.rag.ingest import delete_documents

            if seeded:
                delete_documents(seeded)
        except Exception:
            pass
        with contextlib.suppress(Exception):
            logger.remove(handler)


if __name__ == '__main__':
    raise SystemExit(main())
