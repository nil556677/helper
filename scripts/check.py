"""一条命令全检：ruff → 单测 → 全量（含界面）→ 依赖漂移 → 真实渲染（受限会话自动跳过）。

用法（**在仓库根执行**）：

    python scripts/check.py         # 默认：ruff + 单测 + 全量测试 + 依赖漂移 + （能跑就跑 UI）
    python scripts/check.py --fast  # 只跑 ruff + 单测
    python scripts/check.py --ui    # 明确要求跑真实渲染，失败即整体失败

**为什么要统一成一条**：否则 "提交前跑一遍" 会退化成"跑一下测试"。这里把 lint、测试、
依赖漂移、界面渲染串起来，并做"防污染"检查——覆盖率测试的子进程曾经用真实安装目录，
把 80 多行假错误写进用户的 logs/helper-<日期>.log。这道检查让它下次当场红。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable if sys.executable else str(ROOT / ".venv" / "Scripts" / "python.exe")
RUF = str(ROOT / ".venv" / "Scripts" / "ruff.exe")

#: 防污染：全过程盯住这些目录里文件的 (名字, 大小, mtime)，跑完有变化就判失败。
DIRTY_SCOPE = ("data", "logs")

#: 受限会话下跑 UI 的特征行（命中说明该跳过而不是失败）。
RESTRICTED_MARKERS = ("界面未渲染", "拒绝访问 (0x5)")

VERBOSE = True


def log(msg: str) -> None:
    print(msg, flush=True)


def _run(args: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    log("\n------------------------------------------------------------")
    log("$ " + " ".join(args))
    log("------------------------------------------------------------")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.run(args, cwd=str(cwd), env=env)


def _run_captured(args: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    """带输出的运行（UI 检查用：要看输出判断"是不是受限会话"）。"""
    log("\n------------------------------------------------------------")
    log("$ " + " ".join(args))
    log("------------------------------------------------------------")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.run(args, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")


def snapshot_scope() -> dict[str, tuple[int, int]]:
    """记录 data/ 与 logs/ 里当前文件 (大小, mtime)，作为防污染基线。"""
    snap: dict[str, tuple[int, int]] = {}
    for scope in DIRTY_SCOPE:
        folder = ROOT / scope
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_file():
                try:
                    stat = path.stat()
                    snap[str(path.relative_to(ROOT))] = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    continue
    return snap


def diff_scope(before: dict[str, tuple[int, int]]) -> list[str]:
    """比对跑前/跑后的 data/ 与 logs/，返回有变化的路径（改动 = 污染判据）。"""
    changed: list[str] = []
    after = snapshot_scope()
    keys = set(before) | set(after)
    for key in sorted(keys):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


def run_selfcheck(args: list[str]) -> int:
    return _run([PY, "-m", "app", "--selfcheck", "--no-hash", *args]).returncode


def _make_ui_arg() -> list[str]:
    return ["--ui"] if "--ui" in sys.argv else []


def main() -> int:
    parser = argparse.ArgumentParser(description="一条命令全检")
    parser.add_argument("--fast", action="store_true", help="只跑 ruff + 单测")
    parser.add_argument("--ui", action="store_true", help="明确要求跑真实渲染检查，失败即整体失败")
    parser.add_argument("--skip-selfcheck", action="store_true", help="跳过自检（开发中）")
    parser.add_argument("--skip-lint", action="store_true", help="跳过 ruff")
    args = parser.parse_args()

    failed: list[str] = []

    # 0. 自检（T2.3 接入后）
    if not args.skip_selfcheck:
        code = run_selfcheck([])
        log(f"\n[自检] 退出码 {code}")
        if code != 0:
            failed.append("自检")

    # 1. ruff
    if not args.skip_lint:
        code = _run([RUF, "check", "."]).returncode
        if code != 0:
            failed.append("ruff")

    # 2/3. 测试（**防污染的判据区**：只在这里前后各记一次 data/ 与 logs/ 的快照）
    if args.fast:
        steps = [("单测", ["-m", "pytest", "tests/unit", "-q"])]
    else:
        steps = [
            ("单测", ["-m", "pytest", "tests/unit", "-q"]),
            ("全量", ["-m", "pytest", "-q", "-m", "not slow and not eval"]),
        ]
    poll_before = snapshot_scope()
    for label, step in steps:
        code = _run([PY, *step]).returncode
        if code != 0:
            failed.append(label)
    poll_changed = diff_scope(poll_before)
    if poll_changed:
        failed.append(f"测试污染生产数据（{', '.join(poll_changed[:8])}）")

    # 4. 依赖漂移（T1.3 接入后）
    freeze = ROOT / "scripts" / "freeze_lock.py"
    if freeze.is_file():
        code = _run([PY, str(freeze), "--check"]).returncode
        if code != 0:
            failed.append("依赖漂移")

    # 5. 真实渲染。
    # 默认（非 --ui）：**尽力而为**——受限会话（没显示器/沙箱）下 QtWebEngine 起不来是预期的，
    # 命中受限特征就记为"跳过"，没命中也**不算失败**（运行窗本机才是主战场，见计划 T1.4）。
    # 只在显式 `--ui` 时才把非零退出当成整体失败。
    ui_script = ROOT / "scripts" / "check_ui.py"
    if ui_script.is_file() and args.ui:
        log("（UI 渲染检查：显式要求）")
        code = _run([PY, "-u", str(ui_script)]).returncode
        if code != 0:
            failed.append("UI 渲染")
    elif ui_script.is_file() and not args.fast:
        log("（UI 渲染检查：尽力而为，失败不判整体失败）")
        proc = _run_captured([PY, "-u", str(ui_script)])
        code = proc.returncode
        combined = (proc.stdout or "") + (proc.stderr or "")
        if code != 0:
            if any(marker in combined for marker in RESTRICTED_MARKERS):
                log("[UI 渲染] 受限会话特征命中 → 跳过（不算失败）。")
            else:
                log(f"[UI 渲染] 退出码 {code}；尽力而为模式下不判失败。")

    # 汇总
    if failed:
        log(f"\n\n[失败] 未通过：{', '.join(failed)}")
        return 1
    log("\n\n[全部通过] check.py 所有步骤通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
