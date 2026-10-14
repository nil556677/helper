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
