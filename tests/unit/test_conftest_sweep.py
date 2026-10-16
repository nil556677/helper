"""临时目录清扫：**不许踩别的会话**（并发跑 pytest 时的真实故障）。

**背景（实测踩过）**：两个 pytest 会话同时在跑时，各自的开场/收场清扫会把
`.tmp/test_*-*` 无差别删掉——包括**另一个会话正在用的**目录。症状是子进程写结果文件时
`FileNotFoundError`，看起来像随机失败（`test_log_coverage` 的子进程就中过招）。

修法：临时目录名里本来就带 pid（`<用例名>-<pid>`），清扫时**跳过还活着的进程的目录**。
这里钉住三件事：pid 解析、存活探测（尤其"不能误杀"）、以及"活会话的目录不被删"。
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.conftest import _dir_pid, _pid_alive, _sweep_stale


def test_dir_pid_parses_trailing_number() -> None:
    assert _dir_pid("test_foo_bar-1234") == 1234
    assert _dir_pid("no-pid-here") is None
    assert _dir_pid("weird-") is None


def test_pid_alive_detects_self_and_missing() -> None:
    """自己的进程活着；一个几乎不可能存在的 pid 是死的。"""
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(999_999) is False
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False


def test_sweep_keeps_other_live_session_dirs(fake_root: Path) -> None:
    """**另一个活着的会话的临时目录不许删**（并发跑 pytest 时正是它被误删）。"""
    import subprocess
    import sys

    from app.core import paths

    # 真起一个活着的子进程来代表"另一个正在跑的会话"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        paths.ensure_dirs()
        other = paths.tmp_dir() / f"test_other_session-{child.pid}"
        other.mkdir(parents=True, exist_ok=True)

        _sweep_stale()

        assert other.is_dir(), "另一个活着的会话的临时目录被清掉了"
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_sweep_removes_dead_session_dirs(fake_root: Path) -> None:
    """死掉的会话（强杀/Ctrl+C 留下）的目录要清掉——否则 `.tmp/` 一轮轮长大。"""
    from app.core import paths

    paths.ensure_dirs()
    stale = paths.tmp_dir() / "test_leftover-999999"  # 这个 pid 不会存在
    stale.mkdir(parents=True, exist_ok=True)

    assert _sweep_stale() >= 1
    assert not stale.exists()


def test_sweep_removes_dirs_without_pid(fake_root: Path) -> None:
    """名字里没有 pid 的老式残留目录，按"过期"清掉。"""
    from app.core import paths

    paths.ensure_dirs()
    old = paths.tmp_dir() / "test_legacy-dir"
    old.mkdir(parents=True, exist_ok=True)

    _sweep_stale()

    assert not old.exists()
