"""供 `nicegui.testing` 的 `user` 装置使用的"主文件"。

**为什么不用 `app/__main__.py`**：那个文件被执行时会进 `main()`——去建 Qt 窗口、起本地服务；
测试里既不需要也不该有窗口。这里只做一件事：**把页面注册到 NiceGUI**。

它由 pytest 的 `main_file` 配置项指定（见 pytest.ini），装置会用 ``runpy`` 执行它，
所以这里的模块级代码每次都会跑一遍（这正是"每个用例拿到干净页面"的原因）。
"""

from __future__ import annotations

from nicegui import ui

from app.bootstrap import ensure_ready
from app.nicegui_patch import skip_unused_process_pool
from app.ui.shell import build_shell

# 与真实入口一样先做启动初始化（目录 / 日志 / 引导配置 / 建表）；
# 数据目录由 tests/ui/conftest.py 隔离到 .tmp/，不会碰真实 data/
ensure_ready()

# 去掉 NiceGUI 那个用不到的进程池：不然它会建命名管道，受限沙箱下服务直接起不来
skip_unused_process_pool()


@ui.page('/')
def index() -> None:
    """首页：与 `app/__main__.py` 里注册的那个完全一致。"""
    build_shell()


# 装置在模拟模式下会让 `ui.run(...)` 只做"建 app + 设 storage secret"然后立刻返回
# （见 nicegui/ui_run.py 的 is_user_simulation 分支），所以这里**必须**调用它，
# 否则装置会报 "You must call ui.run() to start the server."
ui.run(show=False, reload=False, native=False, dark=False)
