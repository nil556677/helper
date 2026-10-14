"""界面测试的公共装置。

**用 NiceGUI 自带的装置**（`nicegui.testing` 的 `user` fixture）：它在**进程内**模拟一个浏览器
用户（开页 / 点 / 输入 / 断言"看得到什么"），不需要真的起浏览器或 Qt 窗口——所以跑得快，
也能进 CI。它只匹配**可见**元素，所以"切模式后聊天内容必须消失"这类断言是可信的。

**数据目录必须隔离**：界面测试会真的建表、真的读配置，绝不能碰你的 `data/`。
这里在 import 期就把 `HELPER_DATA_DIR` 指到 `.tmp/ui-test-data`
（`app.core.paths.data_dir()` 每次调用都读这个变量，只要早于第一次使用就生效）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TMP = ROOT / ".tmp"
UI_DATA = TMP / "ui-test-data"
UI_DATA.mkdir(parents=True, exist_ok=True)
os.environ["HELPER_DATA_DIR"] = str(UI_DATA)

#: **把 Python 的临时目录也钉进 `.tmp/`**。两个原因：
#: ① NiceGUI 的测试装置用 `tempfile.mkdtemp(prefix="nicegui-test-storage-")`；
#:    当 TEMP 不可用时 Python 会**退回到当前工作目录** → 仓库根目录里堆一堆
#:    `nicegui-test-storage-*` 空目录（实测出现过 9 个，而且因为工作区根上有系统的删除保护，
#:    事后连删都删不掉）；
#: ② 系统 TEMP 下 `mkdtemp` 建的 0o700 目录在受限令牌下列不出也删不掉（WinError 5）。
tempfile.tempdir = str(TMP)


def _sweep_storage_dirs() -> None:
    """清掉 NiceGUI 装置在 `.tmp/` 下建的 storage 临时目录（每跑一次测试会多一个）。"""
    for path in TMP.glob("nicegui-test-storage-*"):
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def tidy_temp_storage() -> Iterator[None]:
    """会话前后各扫一次（前一次是清历史遗留，后一次是清本次）。"""
    _sweep_storage_dirs()
    yield
    _sweep_storage_dirs()


@pytest.fixture(autouse=True)
def storage_inside_workspace() -> Iterator[None]:
    """把 NiceGUI 测试装置的 storage 目录挪进仓库 ``.tmp/``。

    为什么必须挪：装置用 ``tempfile.mkdtemp`` 在系统 TEMP 下建 0o700 目录，
    而受限令牌下**列不出也删不掉**那种目录（本机实测：``app.reset()`` 清理时 WinError 5），
    结果是每个界面用例都 ERROR。放进工作区就没有这个限制（方案 §11 第 7 条同源）。
    """
    from nicegui import app
    from nicegui.storage import Storage

    target = ROOT / ".tmp" / "nicegui-storage"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    Storage.path = target
    storage = getattr(app, "storage", None)
    if storage is not None:
        storage.path = target
    yield


@pytest.fixture(autouse=True)
def fresh_conversations() -> Iterator[None]:
    """每个界面用例都从"只有一个示例会话"开始。

    会话是**模块级内存态**，会跨用例残留；而且上一轮界面注册的订阅回调指向已删除的元素，
    不清掉的话下一次通知就会报错（``user`` 装置遇到 ERROR 日志会直接判失败）。
    """
    from app.ui.state import conversations

    conversations.reset()
    yield


@pytest.fixture(autouse=True)
def empty_database() -> Iterator[None]:
    """每个界面用例都从**空库**开始（任务、资料、模型槽位都会跨用例残留）。

    为什么每次都要清：界面用例断言的是"页面上看得到什么"，而上一条用例留下的任务/资料
    会直接改变计数与分页区间（踩过：断言"3 条 · 显示 1–3"却看到 4 条）。
    """
    from app.bootstrap import ensure_ready
    from app.core.db.connection import transaction

    ensure_ready()
    with transaction() as conn:
        conn.execute("DELETE FROM llm_slots")
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM documents")  # 片段跟着外键级联删
    yield
