"""去掉 NiceGUI 启动时那个**我们用不到**的进程池。

`ui.run()` 启动时会走 `nicegui.run.setup()`，它建一个 `ProcessPoolExecutor`（给 `run.cpu_bound` 用）。
我们从不用 `cpu_bound`，但在 Windows 上这个池子必然建**命名管道**——而受限沙箱下建命名管道会被
直接拒绝（`PermissionError`，WinError 5），结果是 **NiceGUI 服务根本起不来**（实测：
界面测试全部 ERROR、窗口报"界面服务 20s 内未就绪"）。

所以启动前把 `setup` 换成空操作：

- 少一个用不到的进程池：启动更快，也少一个受限环境下的失败点；
- `run.io_bound`（我们用它跑模型探测，避免卡界面）走的是**独立的线程池**，不受影响；
- `run.cpu_bound` 以后真要用时，NiceGUI 里有懒建路径（`process_pool is None` 时自己建），也不受影响。

**注意**：这个改动只解决"测试/服务起不来"。窗口本身用 QtWebEngine 渲染，Chromium 的
浏览器↔渲染进程通信也走命名管道，那部分**绕不开**——所以窗口与 DOM 断言仍然要在放宽的环境里跑。
"""

from __future__ import annotations

_patched = False


def skip_unused_process_pool() -> None:
    """把 NiceGUI 的 ``run.setup`` 换成空操作（幂等，可重复调用）。"""
    global _patched
    if _patched:
        return
    from nicegui import run

    run.setup = lambda: None  # type: ignore[assignment]
    _patched = True
