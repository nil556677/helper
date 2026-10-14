"""测试公共装置。

**为什么重定义 ``tmp_path``**：受限令牌下列不出也写不进 ``tempfile.mkdtemp`` 建的 0o700 目录，
pytest 默认的 ``tmp_path`` 在这种环境里会直接失败（表现是"用例莫名报 PermissionError"）。
统一改用仓库内 ``.tmp/``，跑完即删。
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest


def _safe_name(name: str) -> str:
    """把用例名变成合法目录名（参数化用例名里会带 ``[`` ``]`` ``/`` 这类字符）。"""
    return re.sub(r"[^0-9A-Za-z_.\-]+", "_", name)[:60] or "case"


def _tests_tmp() -> Path:
    """测试临时目录 —— **只认这一处**：`paths.tmp_dir()`（即仓库内 `.tmp/`）。

    以前这里与 ``tmp_path`` 装置各写一份路径，两份一旦不一致，清扫就扫不到装置留下的目录。
    """
    from app.core import paths

    return paths.tmp_dir()


def _sweep_stale() -> int:
    """扫掉 `.tmp/test_*-*`（上一轮遗留 / 被强杀留下的）。返回清掉几个。"""
    removed = 0
    for stale in _tests_tmp().glob("test_*-*"):
        shutil.rmtree(stale, ignore_errors=True)
        removed += 1
    return removed


def pytest_sessionstart(session: pytest.Session) -> None:
    """开跑前扫一遍：会话被强杀（关窗口 / Ctrl+C）时用例的 teardown 不会执行，
    `.tmp/` 会一轮一轮长大（实测积过 60 多个、也见过 19 个）。"""
    _sweep_stale()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """跑完再扫一遍：正常结束的会话不该在 `.tmp/` 里留下任何 `test_*` 目录。"""
    _sweep_stale()


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    """仓库内的一次性临时目录（替代 pytest 默认实现）。"""
    base = _tests_tmp() / f"{_safe_name(request.node.name)}-{os.getpid()}"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把"安装目录"指到临时目录：配置、日志、数据库都不会碰到真实项目目录。

    做法是把 ``app.core.paths.project_root`` 换掉——所有路径函数都经过它，
    所以一处替换就够，不需要每个模块各打一个补丁。
    """
    from app.core import paths
    from app.core.config import access as config_access
    from app.core.db import connection

    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "project_root", lambda: root)
    config_access.invalidate()
    connection.close_thread_connection()
    try:
        yield root
    finally:
        connection.close_thread_connection()
        config_access.invalidate()
