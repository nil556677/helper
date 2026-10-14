"""日志系统的行为约束（对应技术方案 §11 的七条）。

最要紧的是 :func:`test_traceback_keeps_local_variables_out` —— 那是**安全红线**：
局部变量里就有 API key 明文，loguru 默认的 ``diagnose=True`` 会把它连同异常栈一起打出来。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from app.core.logging import (
    init_logging,
    install_exception_hooks,
    is_initialized,
    log_file,
    redact,
)


@pytest.fixture(autouse=True)
def _drop_sinks() -> Iterator[None]:
    """每个用例结束时清掉 sink：别让上一个用例的文件句柄活到下一个用例。"""
    yield
    logger.remove()


def _read(path: Path) -> str:
    """读日志文件（sink 是同步写，调用返回后内容已经落盘）。"""
    return path.read_text(encoding="utf-8")


def test_init_creates_file_and_writes_message(tmp_path: Path) -> None:
    target = tmp_path / "helper.log"
    assert init_logging("DEBUG", log_file=target, console_level="ERROR") == target

    logger.info("你好，日志")

    assert "你好，日志" in _read(target)
    assert is_initialized()
    assert log_file() == target


def test_init_twice_does_not_duplicate_lines(tmp_path: Path) -> None:
    """幂等：重复初始化不能把一行日志写两份。"""
    target = tmp_path / "dup.log"
    init_logging("DEBUG", log_file=target, console_level="ERROR")
    init_logging("DEBUG", log_file=target, console_level="ERROR")

    logger.info("只该出现一次")

    assert _read(target).count("只该出现一次") == 1


def test_traceback_keeps_local_variables_out(tmp_path: Path) -> None:
    """红线：异常栈不许带局部变量。

    这里故意在抛异常的函数里留一个装着"密钥"的局部变量——一旦有人把某个 sink 的
    ``diagnose`` 漏成默认值（True），密钥就会出现在日志里，用户一截图就等于泄密。
    """
    target = tmp_path / "trace.log"
    init_logging("DEBUG", log_file=target, console_level="ERROR")
    secret = "sk-" + "a1b2c3d4e5f6g7h8"

    def boom() -> None:
        token = secret  # noqa: F841 - 故意留一个带密钥的局部变量
        raise RuntimeError("故意的异常")

    try:
        boom()
    except RuntimeError:
        logger.exception("调用失败")

    text = _read(target)
    assert "故意的异常" in text, "异常本身要记下来"
    assert secret not in text, "局部变量的值绝不能进日志"


def test_redact_masks_common_key_shapes() -> None:
    """兜底脱敏：sk- / api_key= / Bearer 三种常见形态都要挡住。"""
    assert "sk-***" in redact("key=" + "sk-" + "x" * 24)
    assert "abcdefghijklmnop" not in redact("api_key=abcdefghijklmnop")
    assert "abcdefghijklmnop" not in redact("Authorization: Bearer abcdefghijklmnop")


def test_console_sink_keeps_local_variables_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """控制台 sink 也必须关掉 diagnose（它和文件 sink 是两个独立配置）。

    用一个替身 stderr 抓控制台输出——日志里出现密钥最常见的路径就是"文件里擦了、
    控制台忘了擦"。
    """
    target = tmp_path / "console.log"
    init_logging("DEBUG", log_file=target, console_level="DEBUG")
    secret = "sk-" + "z" * 20

    def boom() -> None:
        token = secret  # noqa: F841 - 故意
        raise RuntimeError("控制台里的异常")

    try:
        boom()
    except RuntimeError:
        logger.exception("调用失败")

    captured = capsys.readouterr()
    assert secret not in captured.err
    assert secret not in _read(target)


def test_exception_hooks_send_uncaught_error_to_file(tmp_path: Path) -> None:
    """没人接的异常（主线程）要进日志，而不是只丢给 stderr。

    桌面程序没有常驻控制台；没有这个钩子，崩溃现场就等于没有。
    """
    target = tmp_path / "hook.log"
    init_logging("DEBUG", log_file=target, console_level="ERROR")
    install_exception_hooks()

    try:
        raise ValueError("钩子里的异常")
    except ValueError:
        sys.excepthook(*sys.exc_info())

    assert "钩子里的异常" in _read(target)


def test_unwritable_log_dir_falls_back_to_console(tmp_path: Path) -> None:
    """日志文件写不进去：返回 None（只有控制台 sink），但不抛异常、不影响启动。"""
    blocked = tmp_path / "as-a-file"
    blocked.write_text("我是文件，不是目录", encoding="utf-8")

    assert init_logging("DEBUG", log_file=blocked / "helper.log") is None
    assert log_file() is None


def test_purge_old_logs_keeps_recent_and_removes_stale(fake_root: Path) -> None:
    """只留最近 7 天：过期文件删掉，新文件留着（用户报"日志占地方"时靠这条）。"""
    import os
    import time

    from app.core import paths
    from app.core.logging import purge_old_logs

    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    stale = logs / "console-2026-01-01.log"
    fresh = logs / "console-2026-10-14.log"
    stale.write_text("很久以前", encoding="utf-8")
    fresh.write_text("刚刚", encoding="utf-8")
    old = time.time() - 9 * 86400
    os.utime(stale, (old, old))

    assert purge_old_logs() == 1
    assert not stale.exists(), "超过 7 天的要删"
    assert fresh.exists(), "7 天内的不能动"


def test_purge_old_logs_counts_nothing_when_all_fresh(fake_root: Path) -> None:
    """都是新的：返回 0，一个都不删。"""
    from app.core import paths
    from app.core.logging import purge_old_logs

    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "console-2026-10-14.log").write_text("新的", encoding="utf-8")

    assert purge_old_logs() == 0


def test_sniff_reports_restricted_when_window_never_rendered(fake_root: Path) -> None:
    """受限嗅探：原生输出里有特征行、且上次"界面服务就绪"之后没有"界面已渲染" → 给出结论。"""
    from app.core import paths
    from app.core.logging import LOG_FILENAME, console_file, sniff_restricted_hint

    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    console_file().write_text(
        "[123:456:ERROR:gpu_channel_manager.cc:920] Failed to create GLES3 context\n"
        "[123:456:FATAL::82] Check failed: . : 拒绝访问。 (0x5)\n",
        encoding="utf-8",
    )
    (logs / LOG_FILENAME).write_text(
        "2026-10-14 22:00:00 | INFO | __main__:193 | 界面服务就绪：http://127.0.0.1:1/\n",
        encoding="utf-8",
    )

    hint = sniff_restricted_hint()
    assert hint is not None
    assert "受限环境" in hint and "没有出现「界面已渲染」" in hint


def test_sniff_stays_quiet_without_markers(fake_root: Path) -> None:
    """没有特征行就不要乱猜（干净的日志不该冒出"疑似受限"）。"""
    from app.core import paths
    from app.core.logging import LOG_FILENAME, console_file, sniff_restricted_hint

    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    console_file().write_text("一切正常\n", encoding="utf-8")
    (logs / LOG_FILENAME).write_text("界面服务就绪：…\n界面已渲染（根节点到位）\n", encoding="utf-8")

    assert sniff_restricted_hint() is None


def test_run_id_is_in_every_line(tmp_path: Path) -> None:
    """run id 要进每一行：把"一次启动"的所有记录串起来（用户说"刚才那次"才筛得出来）。"""
    from app.core.logging import RUN_ID

    target = tmp_path / "run.log"
    init_logging("DEBUG", log_file=target, console_level="ERROR")
    logger.info("带 run id 的一行")

    assert RUN_ID in _read(target)


def test_rotation_triggers_on_size_and_new_day(tmp_path: Path) -> None:
    """轮转判据：跨天、或单文件超过上限都要轮（不然一个死循环能把磁盘刷满）。"""
    import os
    import time

    from app.core.logging.setup import ROTATION_SIZE_MB, _needs_rotation

    class _File:
        def __init__(self, path: Path) -> None:
            self.name = str(path)

    small = tmp_path / "small.log"
    small.write_text("x", encoding="utf-8")
    assert _needs_rotation("", _File(small)) is False

    big = tmp_path / "big.log"
    with open(big, "wb") as handle:
        handle.truncate(ROTATION_SIZE_MB * 1024 * 1024)
    assert _needs_rotation("", _File(big)) is True, "超过上限要轮转"

    yesterday = time.time() - 86400
    os.utime(small, (yesterday, yesterday))
    assert _needs_rotation("", _File(small)) is True, "跨天要轮转"


def test_purge_enforces_total_size_limit(fake_root: Path) -> None:
    """总量闸门：即使都在 7 天内，超上限也要从最旧的开始删（防错误风暴堆满磁盘）。"""
    import os
    import time

    from app.core import paths
    from app.core.logging import purge_old_logs

    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    now = time.time()
    made = []
    for index in range(3):
        path = logs / f"console-2026-10-0{index + 1}.log"
        with open(path, "wb") as handle:
            handle.truncate(1024 * 1024)  # 每个 1 MB
        stamp = now - 3600 + index  # 都在 7 天内；index 越小越旧（made[0] 是最旧的）
        os.utime(path, (stamp, stamp))
        made.append(path)

    removed = purge_old_logs(days=7, total_limit_mb=2)

    assert removed == 1, "3 MB 超过 2 MB 上限，应删最旧的一个"
    assert not made[0].exists()
    assert made[1].exists() and made[2].exists()


def test_purge_never_deletes_the_active_log(tmp_path: Path, fake_root: Path) -> None:
    """正在写的主日志永不被清（删了它，日志立刻开始报句柄错误）。"""
    import os
    import time

    from app.core import paths
    from app.core.logging import purge_old_logs

    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    active = logs / "helper.log"
    init_logging("DEBUG", log_file=active, console_level="ERROR")

    old = time.time() - 30 * 86400          # 连保留期都超了
    os.utime(active, (old, old))

    purge_old_logs(days=7, total_limit_mb=0)  # 两道闸门都想删它

    assert active.exists(), "当前正在写的日志不能被删"


def test_daily_sink_writes_into_a_date_named_file(tmp_path: Path) -> None:
    """按天 sink：文件名里就是日期（和 console-<日期>.log 同一套规则）。"""
    from app.core.logging.setup import DailyFileSink

    sink = DailyFileSink(tmp_path, clock=lambda: "2026-10-14")
    sink.write("第一行\n")

    assert (tmp_path / "helper-2026-10-14.log").read_text(encoding="utf-8") == "第一行\n"
    assert sink.path == tmp_path / "helper-2026-10-14.log"
    sink.stop()


def test_daily_sink_switches_file_when_the_day_changes(tmp_path: Path) -> None:
    """跨天自动换文件（loguru 自带的 rotation 做不到改文件名）。"""
    from app.core.logging.setup import DailyFileSink

    today = {"value": "2026-10-14"}
    sink = DailyFileSink(tmp_path, clock=lambda: today["value"])
    sink.write("今天\n")
    today["value"] = "2026-10-15"
    sink.write("明天\n")

    assert (tmp_path / "helper-2026-10-14.log").read_text(encoding="utf-8") == "今天\n"
    assert (tmp_path / "helper-2026-10-15.log").read_text(encoding="utf-8") == "明天\n"
    sink.stop()


def test_daily_sink_rolls_within_the_day_on_size(tmp_path: Path) -> None:
    """同一天超过单文件上限：换个序号继续写，不覆盖也不丢。"""
    from app.core.logging.setup import DailyFileSink

    sink = DailyFileSink(tmp_path, max_bytes=10, clock=lambda: "2026-10-14")
    sink.write("0123456789")   # 正好到上限 → 下一次写入前轮转
    sink.write("abcdefghij")

    assert (tmp_path / "helper-2026-10-14.log").read_text(encoding="utf-8") == "0123456789"
    assert (tmp_path / "helper-2026-10-14-2.log").read_text(encoding="utf-8") == "abcdefghij"
    sink.stop()


def test_purge_removes_old_daily_logs_and_keeps_todays(fake_root: Path) -> None:
    """按天文件同样受 7 天保留约束（两类日志一套规则）。"""
    import os
    import time

    from app.core import paths
    from app.core.logging import purge_old_logs

    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    old_day = logs / "helper-2026-10-01.log"
    today = logs / "helper-2026-10-14.log"
    old_day.write_text("旧", encoding="utf-8")
    today.write_text("新", encoding="utf-8")
    stale = time.time() - 8 * 86400
    os.utime(old_day, (stale, stale))

    assert purge_old_logs() == 1
    assert not old_day.exists()
    assert today.exists()
