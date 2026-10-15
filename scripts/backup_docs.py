"""docs/ 的"不公开"最低成本备份（T4.2）。

**为什么需要它**：`docs/` 被 `.gitignore` 排除（内部文档不公开），于是
`技术方案.md`（700+ 行）等文档**只存在于这台机器上**。日常开发里的 `git clean`
或 `.tmp` 清理都有可能把它们带走，而 git 历史里也找不到副本 —— 这是一份没有
版本保护、且丢了就没法恢复的资料。本脚本把它们递归复制到**仓库之外**的目标目录，
并保留最近若干份快照，避免历史文档被覆盖。

用法（**在仓库根执行**）：

    .venv\\Scripts\\python.exe scripts\\backup_docs.py                 # 默认目标
    .venv\\Scripts\\python.exe scripts\\backup_docs.py --to X\\Y       # 指定目标
    .venv\\Scripts\\python.exe scripts\\backup_docs.py --keep 7        # 保留 7 份
    .venv\\Scripts\\python.exe scripts\\backup_docs.py --dry-run       # 只看会做什么

保留策略：目标目录下每个快照是 `<时间戳>`（形如 ``20261015-193025``）目录；
按目录名排序后只留最近 ``--keep`` 份，删掉更旧的。**只删目录名匹配时间戳模式的
目录**（也就是本脚本自己备份出来的），绝不会碰其它东西。

建议时机：大改文档前后各跑一次。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: 脚本所在目录上一级就是仓库根（scripts/ 直接在仓库根下）
ROOT = Path(__file__).resolve().parents[1]
DOCS_SRC = ROOT / "docs"

#: 默认目标：仓库之外（`..\\helper-docs-backup`），避开 git clean 与 .tmp 清理
DEFAULT_TO = ROOT.parent / "helper-docs-backup"

#: 快照目录名的规范式样：YYYYMMDD-HHMMSS。
#: 同一秒内连跑两次会撞名，此时用 ``-2`` ``-3`` 后缀错开；命名的正则把后缀也算进来，
#: 所以这种目录同样能被"自己是备份"识别、参与保留/清理。
_BACKUP_NAME_RE = re.compile(r"(?:\d{8}-\d{6})(?:-\d+)?")

#: 明显临时文件：macOS 的元数据、Office 的 `~$` 锁定文件（复制它们纯属浪费）。
_TEMP_FILES = (".DS_Store",)


@dataclass
class BackupResult:
    """一次备份的结果：目标路径、本次复制的总字节数、被删掉的旧快照目录。"""

    target: Path
    total_bytes: int
    removed: list[Path]
    dry_run: bool


def is_backup_dir(name: str) -> bool:
    """判断一个目录名是否为本脚本备份出来的快照（避免误删他人目录）。"""
    return _BACKUP_NAME_RE.fullmatch(name) is not None


def skip_file(name: str) -> bool:
    """该文件是否属于"明显临时文件"，复制时应跳过。"""
    return name in _TEMP_FILES or name.startswith("~$")


def collect_files(source: Path) -> list[Path]:
    """收集 source 下所有应复制的文件（跳过明显临时文件）。"""
    return [
        p
        for p in sorted(source.rglob("*"))
        if p.is_file() and not skip_file(p.name)
    ]


def _new_backup_dir(to_dir: Path, timestamp: str) -> Path:
    """在同一秒内连跑时错开（``timestamp`` → ``timestamp-2`` → ...），
    避免两个快照写进同一个目录。"""
    dest = to_dir / timestamp
    n = 2
    while dest.exists():
        dest = to_dir / f"{timestamp}-{n}"
        n += 1
    return dest


def _discover_backups(to_dir: Path) -> list[Path]:
    """目标目录下所有本脚本自己备份出来的快照目录（按名字/时间排序）。"""
    if not to_dir.is_dir():
        return []
    return sorted(
        (d for d in to_dir.iterdir() if d.is_dir() and is_backup_dir(d.name)),
        key=lambda d: d.name,
    )


def _prune_old(to_dir: Path, keep: int) -> tuple[list[Path], list[Path]]:
    """算出该删的旧快照。返回 ``(will_remove, keep_list)``，本函数不实际删除。

    新快照会在本轮落盘，所以容量要先给它留一份：只保留 keep-1 个旧目录。
    """
    backups = _discover_backups(to_dir)
    overflow = len(backups) - (keep - 1)
    if overflow <= 0:
        return [], backups
    removed = backups[:overflow]
    kept = backups[overflow:]
    return removed, kept


def backup_docs(
    source: Path,
    to_dir: Path,
    keep: int = 10,
    dry_run: bool = False,
) -> BackupResult:
    """把 ``source`` 下的文档备份到 ``to_dir`` 的一个新时间戳快照目录里。

    - 仅当 ``dry_run`` 为真时只计算/打印、不写盘不删目录；
    - 保留最近 ``keep`` 份（含本轮新快照），更旧的只在名称为备份快照时才删除。
    """
    if not source.is_dir():
        raise FileNotFoundError(f"源目录不存在：{source}")

    to_remove, _kept = _prune_old(to_dir, keep)
    files = collect_files(source)
    total_bytes = sum(f.stat().st_size for f in files)

    if dry_run:
        return BackupResult(target=Path(), total_bytes=total_bytes, removed=to_remove, dry_run=True)

    to_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = _new_backup_dir(to_dir, timestamp)
    for f in files:
        rel = f.relative_to(source)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, out)

    removed: list[Path] = []
    for old in to_remove:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old)

    return BackupResult(target=dest, total_bytes=total_bytes, removed=removed, dry_run=False)


def main(argv: list[str] | None = None) -> int:
    # Windows 默认 stdout 是 GBK，中文会爆 UnicodeEncodeError；统一按 UTF-8 输出。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="把 docs/ 备份到仓库之外，保留最近若干份快照。"
    )
    parser.add_argument("--to", type=Path, default=DEFAULT_TO, help="目标目录（默认仓库之外的 helper-docs-backup）")
    parser.add_argument("--keep", type=int, default=10, help="保留最近几份快照")
    parser.add_argument("--dry-run", action="store_true", help="只打印会做什么，不写盘不删除")
    args = parser.parse_args(argv)

    result = backup_docs(DOCS_SRC, args.to, keep=args.keep, dry_run=args.dry_run)

    if result.dry_run:
        print("（dry-run，未实际写入）")
        print(f"将复制 {result.total_bytes} 字节到 {args.to} 下的新时间戳快照目录")
    else:
        print(f"本次目标路径：{result.target}")
        print(f"本次总字节数：{result.total_bytes}")
    for old in result.removed:
        action = "将删除旧快照" if result.dry_run else "已删除旧快照"
        print(f"{action}：{old}")
    if not result.removed:
        print("无需清理旧快照" if result.dry_run else "不需要清理旧快照")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
