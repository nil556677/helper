"""自检：`python -m app --selfcheck` —— 所有远程排查的入口。

**为什么做成独立模块**：一条命令给出**可复制的环境报告**，且**不需要 Qt**（受限会话里也能跑）。
这正是 `scripts/check.py` 的第 0 步。

**铁律：自检只读，不改状态。**

- 数据库用 ``file:...?mode=ro`` **只读**打开，只 ``PRAGMA quick_check``，不跑迁移、不建表；
- 不写 ``config.json``、不动 ``settings`` 表；
- 日志：允许写（它本身就是日志系统的一部分），但**不把自检结果写进 settings**。

否则"我跑个自检看看"本身就可能改动用户数据，这在支持场景里是最忌讳的。
"""

from __future__ import annotations

import contextlib
import json
import socket
import sys
from dataclasses import dataclass

from loguru import logger

from app.core import paths
from app.core.config import access
from app.core.db import schema
from app.core.logging import (
    check_disk_space,
    environment_snapshot,
    init_logging,
    is_initialized,
)
from app.core.models import files as model_files

#: 各检查项的名字（保持顺序稳定，人读 / 机器读都对得上）。
CHECK_NAMES = (
    "目录",
    "配置",
    "数据库",
    "模型文件",
    "端口",
    "磁盘",
    "日志",
    "单实例",
    "环境快照",
)

#: 退出码规约（文档 T2.3）。
EXIT_ALL_OK = 0
EXIT_HAS_ERROR = 1
EXIT_SELF_ERROR = 2


@dataclass(frozen=True)
class CheckResult:
    """一项检查的结果。"""

    level: str  # ERROR / WARNING / INFO
    name: str
    detail: str
    hint: str = ""


_LEVEL_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2, "OK": 3}


def run_checks(*, deep_hash: bool = True, check_config: bool = True) -> list[CheckResult]:
    """跑全部检查（8 项 + 环境快照），返回结果列表。

    参数：
        deep_hash: 模型哈希是否要深查（``--no-hash`` 传 False）。
        check_config: 是否连"配置能加载"一起查（config.json 坏掉时默认也要报告）。
    """
    return _directories() + _config(check_config) + _database() + _models(deep_hash) + _port() + (
        _disk() + _log_sink() + _single_instance() + [_environment(snapshot())]
    )


# -- 每一项 -----------------------------------------------------------------


def _check(level: str, name: str, detail: str, hint: str = "") -> CheckResult:
    return CheckResult(level=level, name=name, detail=detail, hint=hint)


def _directories() -> list[CheckResult]:
    results: list[CheckResult] = []
    import os

    for label, directory in (("数据目录", paths.data_dir()), ("日志目录", paths.logs_dir()), ("模型目录", paths.models_dir())):
        if not directory.is_dir():
            results.append(_check("ERROR", "目录", f"{label} 不存在：{directory}"))
            continue
        # 只读判据：不写文件（自检铁律"不改状态"）。`os.access` 在本机受限令牌下
        # 对"能建文件但 fstat 说可写"的目录会保守一点，但对"只读位置"能可靠报 ERROR。
        if not os.access(directory, os.W_OK):
            results.append(_check("ERROR", "目录", f"{label} 不可写（os.access 拒绝）：{directory}"))
            continue
    if not any(item.level == "ERROR" for item in results):
        results.append(_check("OK", "目录", "data/、logs/、models/ 存在且可写"))
    return results


def _config(check_config: bool) -> list[CheckResult]:
    if not check_config:
        return []
    try:
        config = access.load_fresh()
        # 引导配置在数据库里；库还没建时按默认值报告。老 config.bad-* 只作迁移线索。
        bad = sorted(paths.project_root().glob("config.bad-*.json"))
        detail = (
            f"引导配置可加载（数据库 settings / 日志级别 {config.log_level}）"
            + (f"；检测到已隔离的坏配置 {bad[0].name}" if bad else "")
        )
        return [_check("WARNING" if bad else "OK", "配置", detail)]
    except Exception as exc:
        return [_check("ERROR", "配置", f"配置加载失败：{type(exc).__name__}: {exc}")]


def _database() -> list[CheckResult]:
    db_path = paths.db_file()
    if not db_path.exists():
        return [_check("ERROR", "数据库", f"库文件不存在：{db_path}")]
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            version = schema.schema_version(conn)
            row = conn.execute("PRAGMA quick_check").fetchone()
            integrity = str(row[0]) if row else "?"
            if integrity != "ok":
                return [_check("ERROR", "数据库", f"PRAGMA quick_check 返回 {integrity}")]
            return [_check("OK", "数据库", f"只读打开正常，schema v{version}，一致性 ok")]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [_check("ERROR", "数据库", f"打不开（readonly）：{type(exc).__name__}: {exc}")]


def _models(deep: bool) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name in model_files.list_names():
        issues = model_files.problems(name, deep=deep)
        if not issues:
            results.append(_check("OK", "模型文件", f"{name}：齐全"))
            continue
        text = "；".join(issues)
        # 大小对、只是哈希不符 → 可能是用户自己量化的，当 WARNING；否则（缺/大小）ERROR
        all_text = "".join(issues)
        is_hash_only = "大小不符" not in all_text and "缺失" not in all_text
        results.append(
            _check("WARNING" if (is_hash_only and deep) else "ERROR", "模型文件", f"{name}：{text}")
        )
    if not results:
        results.append(_check("OK", "模型文件", "清单为空"))
    return results


def _port() -> list[CheckResult]:
    host = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            port = sock.getsockname()[1]
        return [_check("OK", "端口", f"{host}:{port} 可绑（拿到空闲端口）")]
    except OSError as exc:
        return [_check("ERROR", "端口", f"{host} 连空闲端口都要不到：{exc}")]


def _disk() -> list[CheckResult]:
    warning = check_disk_space(paths.data_dir())
    if warning:
        return [_check("WARNING", "磁盘", warning)]
    return [_check("OK", "磁盘", "磁盘剩余空间充足")]


def _log_sink() -> list[CheckResult]:
    if is_initialized():
        return [_check("OK", "日志", "日志系统已初始化")]
    # 还没初始化（脚本里直接跑）：探测一下文件 sink 能不能建
    try:
        file = init_logging(console_level="CRITICAL")  # 静默：不刷屏
        if file is None:
            return [_check("WARNING", "日志", "文件 sink 不可用（只有控制台）")]
        return [_check("OK", "日志", f"文件 sink 可用：{file.name if file else ''}")]
    except Exception as exc:
        return [_check("WARNING", "日志", f"日志初始化异常：{type(exc).__name__}: {exc}")]


def _single_instance() -> list[CheckResult]:
    from app.core import single_instance

    ok = single_instance.acquire()
    if ok and single_instance._mutex_handle is not None:
        single_instance.release()
        return [_check("OK", "单实例", "可获取单实例守卫（没有其它实例在跑）")]
    if not ok:
        return [_check("WARNING", "单实例", "互斥体已被占用 → 已有另一个实例在跑（这不一定是坏事）")]
    return [_check("WARNING", "单实例", "守卫不可用（跳过了，不影响启动）")]


def snapshot() -> dict[str, str]:
    """环境快照（复用 logging.environment_snapshot）。"""
    return environment_snapshot()


def _environment(info: dict[str, str]) -> CheckResult:
    return _check("INFO", "环境快照", "；".join(f"{key}={value}" for key, value in info.items()))


def render(results: list[CheckResult]) -> str:
    """把结果渲染成人读报告（对齐、带 ✓/!/✗）。"""
    longest = max((len(item.name) for item in results), default=0)
    lines = ["helper 环境自检", "=" * 60]
    for item in results:
        mark = {"OK": "✓", "INFO": "·", "WARNING": "!", "ERROR": "✗"}.get(item.level, "?")
        lines.append(f"{mark} {item.name:<{longest}} {item.detail}")
        if item.hint:
            lines.append(f"    → {item.hint}")
    errors = sum(1 for item in results if item.level == "ERROR")
    warnings = sum(1 for item in results if item.level == "WARNING")
    lines.append("=" * 60)
    lines.append(f"结论：{errors} 个 ERROR，{warnings} 个 WARNING，退出码 {1 if errors else 0}")
    return "\n".join(lines)


def as_json(results: list[CheckResult]) -> str:
    """渲染成机器读的 JSON。"""
    return json.dumps(
        {
            "ok": all(item.level != "ERROR" for item in results),
            "results": [
                {"level": item.level, "name": item.name, "detail": item.detail, "hint": item.hint}
                for item in results
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def main(json_output: bool = False, *, deep_hash: bool = True) -> int:
    """命令行入口（`--selfcheck` 接线）。"""
    # 输出统一 UTF-8：自检报告带 ✓/✗，而受限/无控制台时 stdout 可能是 GBK，直接 print 会崩。
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        results = run_checks(deep_hash=deep_hash)
    except Exception as exc:
        logger.error("自检进程出错：{}", exc)
        print(exc)
        return EXIT_SELF_ERROR
    print(as_json(results) if json_output else render(results))
    return EXIT_ALL_OK if all(item.level != "ERROR" for item in results) else EXIT_HAS_ERROR
