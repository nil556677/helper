"""P7：子进程守护——拉起 / 重启 / 停止 / 反复崩溃降级。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from app.core.proc import ChildSpec, Supervisor

SLEEPER = [sys.executable, "-c", "import time; time.sleep(5)"]


def _wait_alive(sup: Supervisor, name: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sup.status()[name]["alive"]:
            return True
        time.sleep(0.05)
    return False


def test_supervisor_starts_child(tmp_path: Path) -> None:
    sup = Supervisor([ChildSpec("fake", SLEEPER)], log_dir=tmp_path / "logs", backoff_base=0.0)
    sup.start()
    try:
        assert _wait_alive(sup, "fake")
        assert sup.status()["fake"]["pid"]
    finally:
        sup.stop()


def test_supervisor_restarts_crashed_child(tmp_path: Path) -> None:
    sup = Supervisor([ChildSpec("fake", SLEEPER)], log_dir=tmp_path / "logs", backoff_base=0.0)
    sup.start()
    try:
        assert _wait_alive(sup, "fake")
        sup._children[0].proc.kill()  # 模拟崩溃
        time.sleep(0.1)
        states = sup.ensure_running()
        assert states["fake"] in {"restarted", "running"}
        assert _wait_alive(sup, "fake")
    finally:
        sup.stop()


def test_supervisor_degrades_after_repeated_crashes(tmp_path: Path) -> None:
    sup = Supervisor(
        [ChildSpec("fake", SLEEPER)], log_dir=tmp_path / "logs", max_restarts=1, backoff_base=0.0
    )
    sup.start()
    try:
        # 连续两次崩溃 → 超过 max_restarts=1 → 降级
        for _ in range(2):
            if sup._children[0].proc is not None:
                sup._children[0].proc.kill()
            time.sleep(0.1)
            time.sleep(0.1)
            sup.ensure_running()
        assert sup.status()["fake"]["degraded"] is True
    finally:
        sup.stop()


def test_supervisor_stop_waits_and_reports(tmp_path: Path) -> None:
    """`stop()` 必须等子进程真正退出，并逐个子进程报告停止结果。

    这是主进程退出逻辑的前提：确认每个受管子进程都关了，才放主进程退出；
    且要能区分 ``stopped / killed``（正常关掉）与 ``still-running``（确认不了、留给用户处理）。
    """
    # 短期自持进程：正常 terminate 就能退，不用等它自己超时。
    sup = Supervisor([ChildSpec("fake", SLEEPER)], log_dir=tmp_path / "logs", backoff_base=0.0)
    sup.start()
    assert _wait_alive(sup, "fake")
    report = sup.stop(grace_s=1.0)
    assert "fake" in report
    assert report["fake"] in {"stopped", "killed"}
    # 子进程确实已经没了，而不是停留在 report 里。
    proc = sup._children[0].proc
    assert proc is None or proc.poll() is not None


def test_supervisor_stop_reports_not_started(tmp_path: Path) -> None:
    """从没拉起来（或已经退出的）子进程，stop 应记为 not-started 而不是误报异常。"""
    sup = Supervisor([ChildSpec("fake", SLEEPER)], log_dir=tmp_path / "logs", backoff_base=0.0)
    report = sup.stop(grace_s=0.5)
    assert report == {"fake": "not-started"}
