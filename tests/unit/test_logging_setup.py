"""日志命名与子进程日志初始化：所有日志文件统一 ``<组件>-YYYY-MM-DD.log``。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.core import paths
from app.core.logging.setup import handlers
from app.core.logging.setup.handlers import CHILD_LOG_COMPONENTS, child_log_file


def test_child_log_file_is_dated_and_uses_same_rule(tmp_path: Path) -> None:
    """子进程日志名必须与主日志同一套规则：``<组件>-YYYY-MM-DD.log``。"""
    path = child_log_file("worker", day="2026-10-28", directory=tmp_path)
    assert path.name == "worker-2026-10-28.log"  # 带日期
    assert path.parent == tmp_path  # 目录可注入（测试/自定义）
    for name in CHILD_LOG_COMPONENTS:  # worker / qdrant 都适用
        assert child_log_file(name).parent == paths.logs_dir()  # 默认落 logs/


class _FakeLogger:
    """记录调用、不碰真实 loguru 全局状态的假 logger。"""

    def __init__(self) -> None:
        self.removed = 0
        self.patcher = None
        self.added: list[tuple[object, dict]] = []

    def remove(self) -> None:
        self.removed += 1

    def configure(self, *, patcher=None) -> None:
        self.patcher = patcher

    def add(self, sink, **kwargs) -> None:
        self.added.append((sink, kwargs))

    def info(self, *_args, **_kwargs) -> None:
        pass


def test_init_child_logging_only_installs_stderr_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """子进程日志**只**装 stderr sink（文件由 Supervisor 承接），且用统一格式。

    回归：worker 以前从没初始化日志，落盘用的是 loguru 默认格式（缺 run 字段），
    与 ``helper-*.log`` 字段不一致。
    """
    fake = _FakeLogger()
    monkeypatch.setattr(handlers, "logger", fake)

    handlers.init_child_logging("worker")

    assert fake.removed == 1  # 清掉默认 handler
    assert fake.patcher is not None  # 装了兜底脱敏/字段补全
    assert len(fake.added) == 1, "只能有一个 sink：再多一个就会双写"
    sink, kwargs = fake.added[0]
    assert sink is handlers.sys.stderr  # 只写 stderr
    assert kwargs["format"] == handlers.LOG_FORMAT  # 与主日志同一套格式
    assert kwargs["level"] == "DEBUG"  # 默认 debug
    assert kwargs["diagnose"] is False  # 不带局部变量（防泄漏）


def test_purge_keeps_todays_child_logs(fake_root: Path) -> None:
    """清理旧日志时，**当天的**子进程日志必须保留（子进程还握着它写）。"""
    from app.core.logging.setup import purge_old_logs

    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    old = logs / "worker-2020-01-01.log"
    old.write_text("x", encoding="utf-8")
    today = child_log_file("worker")
    today.write_text("y", encoding="utf-8")
    long_ago = time.time() - 90 * 86400
    os.utime(old, (long_ago, long_ago))  # 过期
    os.utime(today, (long_ago, long_ago))  # 同旧 mtime，验证"按名字保护"而不是按时间

    purge_old_logs(days=7)

    assert not old.exists()  # 过期的删了
    assert today.exists()  # 当天的子日志留着
