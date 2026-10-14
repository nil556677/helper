"""日志覆盖率：**逐层断言"所有错误都记录下来了"**（技术方案 §10.1 的验收依据）。

为什么要用子进程：这个测试要验证的正是"重定向进程的 fd 1/2"与"劫持 stdlib logging"——
在主测试进程里做会把 pytest 自己的输出也劫走。所以子进程跑一遍真实入口的装钩子路径，
把结果写进文件，父进程再逐条核对。

判据是**标记字符串**：每类输出打一条带唯一标记的行，然后看它落在
`logs/helper.log`（通道 1/2）还是 `logs/console-<日期>.log`（通道 3）里。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MARK = "COVER-71C4"

#: 子进程脚本：跑真实入口的日志接线，把各类输出各打一条。
CHILD_SCRIPT = f'''
import asyncio, json, os, sys, threading
from pathlib import Path

ROOT = Path(r"{ROOT}")
sys.path.insert(0, str(ROOT))

from app.bootstrap import ensure_ready

MARK = "{MARK}"
result_path = Path(sys.argv[1])

ensure_ready()

# 真实入口装钩子的那段（fd 捕获 / stdlib 桥 / asyncio 策略 / Qt 消息）
from app.__main__ import _install_log_channels

_install_log_channels()

from loguru import logger

logger.info(f"{{MARK}} loguru")
import logging
logging.getLogger("nicegui").error(f"{{MARK}} stdlib-nicegui")
logging.getLogger("uvicorn.error").warning(f"{{MARK}} stdlib-uvicorn")
os.write(2, f"{{MARK}} native-stderr\\n".encode())
os.write(1, f"{{MARK}} native-stdout\\n".encode())


def crash_in_thread():
    raise RuntimeError(f"{{MARK}} thread-exception")


thread = threading.Thread(target=crash_in_thread, name="cover")
thread.start()
thread.join()


async def crash_in_task():
    raise RuntimeError(f"{{MARK}} asyncio-task")


async def main():
    task = asyncio.get_running_loop().create_task(crash_in_task())
    await asyncio.sleep(0.2)
    del task                     # 触发 "Task exception was never retrieved" → 异常处理器
    await asyncio.sleep(0.2)


asyncio.run(main())

try:
    from PySide6.QtCore import qWarning
    qWarning(f"{{MARK}} qt-warning")
except Exception as exc:
    logger.warning(f"{{MARK}} qt-unavailable {{exc}}")

json.dumps  # noqa: B018  (确保 import 被用到)
result_path.write_text(
    json.dumps({{"run": os.environ.get("HELPER_DATA_DIR", ""), "ok": True}}), encoding="utf-8"
)
print(f"{{MARK}} child-done")
'''


@pytest.fixture
def coverage_run(tmp_path: Path) -> dict[str, str]:
    """跑一次子进程，返回两张日志的全文（主日志 + 当天原生输出）。"""
    script = tmp_path / "child.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")
    result = tmp_path / "result.json"

    completed = subprocess.run(
        [sys.executable, str(script), str(result)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert completed.returncode == 0, f"子进程失败：{completed.stdout}\n{completed.stderr}"

    from app.core.logging import console_file

    main_log = ROOT / "logs" / "helper.log"
    return {
        "main": main_log.read_text(encoding="utf-8", errors="replace") if main_log.exists() else "",
        "console": (
            console_file().read_text(encoding="utf-8", errors="replace")
            if console_file().exists()
            else ""
        ),
    }


@pytest.mark.parametrize(
    ("what", "marker", "channel"),
    [
        ("loguru（我们自己的日志）", "loguru", "main"),
        ("stdlib logging：NiceGUI", "stdlib-nicegui", "main"),
        ("stdlib logging：uvicorn", "stdlib-uvicorn", "main"),
        ("原生 stderr（Qt/Chromium 的 FATAL 走这里）", "native-stderr", "console"),
        ("原生 stdout", "native-stdout", "console"),
        ("子线程未捕获异常", "thread-exception", "main"),
        ("asyncio 任务异常（界面回调）", "asyncio-task", "main"),
        ("Qt 消息处理器", "qt-warning", "main"),
    ],
)
def test_every_layer_reaches_the_log(
    coverage_run: dict[str, str], what: str, marker: str, channel: str
) -> None:
    """每一层都要能被记下来——这是"日志只干一件事"的全部意义。"""
    expected = f"{MARK} {marker}"
    assert expected in coverage_run[channel], (
        f"{what} 没进{'主日志' if channel == 'main' else '原生输出文件'}：\n"
        f"  期望包含：{expected}\n"
        f"  主日志尾部：{coverage_run['main'][-600:]}"
    )


def test_native_output_is_redirected_into_the_daily_console_file(coverage_run: dict[str, str]) -> None:
    """原生输出必须落在 console-<日期>.log：白板那次的现场只在这里。"""
    from app.core.logging import console_file

    assert console_file().name.startswith("console-")
    assert console_file().name.endswith(".log")
    assert f"{MARK} native-stderr" in coverage_run["console"]


def test_startup_snapshot_is_recorded(coverage_run: dict[str, str]) -> None:
    """启动环境快照要写进主日志（版本 / flags / TEMP / 目录）——排查环境问题全靠它。"""
    tail = coverage_run["main"][-4000:]
    assert "环境快照：" in tail
    for key in ("Python=", "TEMP=", "数据目录=", "Chromium 参数="):
        assert key in tail, f"快照里少了 {key}"
