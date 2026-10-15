"""入口里那几个"纯逻辑"辅助函数（跑不起 Qt 也能测）。"""

from __future__ import annotations


def test_js_log_level_mapping() -> None:
    """浏览器控制台级别 → 日志级别：Info 压到 DEBUG（前端 console.log 量太大）。

    这条映射决定"前端报错到底进不进日志"：Warning/Error 必须能进来（默认级别 INFO 也看得见），
    Info 则默认不记。
    """
    from app.__main__ import _js_log_level

    assert _js_log_level("ErrorMessageLevel") == "ERROR"
    assert _js_log_level("WarningMessageLevel") == "WARNING"
    assert _js_log_level("InfoMessageLevel") == "DEBUG"
    assert _js_log_level("未知级别") == "DEBUG"


def test_run_summary_is_logged_once(tmp_path: Path, fake_root: Path) -> None:
    """收尾行：只记一次（Qt 的 aboutToQuit 与 atexit 都会来叫它），内容带上时长与任务数。"""
    import time

    from app.__main__ import _log_run_summary
    from app.core.db import schema
    from app.core.logging import init_logging

    schema.init_db()
    target = tmp_path / "summary.log"
    init_logging("INFO", log_file=target, console_level="ERROR")

    _log_run_summary(time.monotonic() - 60.0, time.time() - 60.0)
    _log_run_summary(time.monotonic() - 60.0, time.time() - 60.0)   # 第二次不该再写

    text = target.read_text(encoding="utf-8")
    assert text.count("本次运行结束") == 1, text
    assert "任务完成" in text and "失败" in text
