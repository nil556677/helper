"""P7：子进程守护——拉起 / 重启 / 停止 / 反复崩溃降级 / 残留清理。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from app.core.proc import ChildSpec, Supervisor, cleanup_stale_children

SLEEPER = [sys.executable, "-c", "import time; time.sleep(5)"]


def _pid_alive(pid: int) -> bool:
    """按句柄判断 pid 是否还活着（不依赖 parent 关系）。"""
    from app.core.proc import supervisor as sup_mod

    return sup_mod._pid_liveness(pid)[0]


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


def test_health_failure_is_debounced_not_killed_immediately(tmp_path: Path) -> None:
    """健康检查失败要做去抖：**一次**失败不能立刻把活着的进程杀掉（防瞬时繁忙误杀）。

    只有连续失败超过 HEALTH_FAIL_LIMIT 次才终止重启。
    """
    from app.core.proc import supervisor as sup_mod

    calls = {"n": 0}

    def bad_health() -> bool:
        calls["n"] += 1
        return False  # 每次都"不健康"

    sup = Supervisor(
        [ChildSpec("fake", SLEEPER, health=bad_health)],
        log_dir=tmp_path / "logs", backoff_base=0.0,
    )
    sup.start()
    try:
        assert _wait_alive(sup, "fake")
        # 越过起步宽限：否则前 60s 一律判 running，测不到去抖逻辑。
        sup._children[0].last_start = time.monotonic() - sup_mod.HEALTH_START_GRACE_S - 1
        # 连续不足阈值次失败：进程必须保持存活，不应被误杀。
        for _ in range(sup_mod.HEALTH_FAIL_LIMIT - 1):
            sup.ensure_running()
            assert sup._children[0].proc.poll() is None  # 还活着
        # 第 threshold 次起允许终止重启——注意重启会拉起新进程，状态变为 restarted。
        state = sup.ensure_running()
        assert state["fake"] in {"restarted", "running"}
        assert calls["n"] > 0  # 健康回调确实被问过了
    finally:
        sup.stop()


def test_stop_reports_still_running_when_unstoppable(tmp_path: Path, monkeypatch) -> None:
    """杀不掉时必须如实报 ``still-running``（不能瞒报成 stopped），交给上层提示用户。"""
    from app.core.proc import supervisor as sup_mod

    monkeypatch.setattr(sup_mod, "_wait_exit", lambda proc, timeout_s: False)  # 永远"等不到退出"
    sup = Supervisor([ChildSpec("fake", SLEEPER)], log_dir=tmp_path / "logs", backoff_base=0.0)
    sup.start()
    try:
        assert _wait_alive(sup, "fake")
        report = sup.stop(grace_s=0.1)
        assert report["fake"] == "still-running"  # 如实上报
    finally:
        proc = sup._children[0].proc
        if proc is not None and proc.poll() is None:
            proc.kill()  # 清场（上面的桩不影响真实 kill）
            proc.wait(timeout=5)


def test_registry_records_child_and_empties_after_stop(tmp_path: Path) -> None:
    """子进程拉起要登记、确认退出后要摘掉，登记表不能越攒越多。"""
    log_dir = tmp_path / "logs"
    sup = Supervisor([ChildSpec("fake", SLEEPER)], log_dir=log_dir, backoff_base=0.0)
    sup.start()
    try:
        assert _wait_alive(sup, "fake")
        registry = tmp_path / "children.json"  # log_dir 的父目录
        assert registry.is_file()  # 拉起即登记
        entries = json.loads(registry.read_text(encoding="utf-8"))["children"]
        assert [e["name"] for e in entries] == ["fake"]  # 名字对得上
        assert isinstance(entries[0]["created"], (int, float))  # 记了创建时刻（防 PID 复用）
    finally:
        report = sup.stop(grace_s=2.0)
    assert report["fake"] in {"stopped", "killed"}
    entries = json.loads((tmp_path / "children.json").read_text(encoding="utf-8"))["children"]
    assert entries == []  # 已摘干净


def test_cleanup_stale_children_kills_leftover(tmp_path: Path) -> None:
    """模拟"上次崩溃、on_quit 没跑"：下次启动照登记表把残留子进程清掉。"""
    log_dir = tmp_path / "logs"
    sup = Supervisor([ChildSpec("fake", SLEEPER)], log_dir=log_dir, backoff_base=0.0)
    sup.start()
    try:
        assert _wait_alive(sup, "fake")
        pid = sup._children[0].proc.pid  # 记下 pid
        result = cleanup_stale_children(log_dir)  # 假装这是下一次启动
        assert result["killed"], result  # 报告清理成功
        assert not result["failed"], result  # 没有清不掉的
        assert not _pid_alive(pid)  # 进程确实没了
    finally:
        proc = sup._children[0].proc
        if proc is not None and proc.poll() is None:
            proc.kill()  # 清场
            proc.wait(timeout=5)


def test_cleanup_skips_reused_pid(tmp_path: Path) -> None:
    """登记表里的 pid 已被别的进程复用时（创建时刻对不上），绝不能下手误杀。"""
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])  # 别的进程
    try:
        path = tmp_path / "children.json"
        path.write_text(  # 伪造一条"pid 相同但创建时刻对不上"的登记
            json.dumps({"children": [{"name": "fake", "pid": proc.pid, "created": 1.0}]}),
            encoding="utf-8",
        )
        result = cleanup_stale_children(tmp_path / "logs")
        assert result == {"killed": [], "failed": []}  # 什么都没动
        assert proc.poll() is None  # 别人的进程还活着
        # 条目被丢弃；全清空时登记文件会被删掉（不留空文件）。
        assert (not path.exists()) or json.loads(path.read_text(encoding="utf-8"))["children"] == []
    finally:
        proc.kill()
        proc.wait(timeout=5)
