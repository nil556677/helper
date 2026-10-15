"""数据备份工具 + 恢复验证（对应基础设施补全计划 T3.3）。

思路一句话：**备份 = 复制 data/ 目录**。所以这里把「数据库 + data/files + userdict.txt」打成一包，
落一张 ``manifest.json``，并给三个动作：

- ``--to <目录>``：做整份备份。数据库用 ``VACUUM INTO``（SQLite 原生一致快照，不用管 WAL 尾巴）；
  ``data/files/`` 逐文件复制；``userdict.txt`` 等小文件一起。写 ``manifest.json``。
- ``--verify <目录>``：重算的大小 / sha256 与 manifest 比对，不一致退出码 1。
- ``--restore-smoke <目录>``：**在工具层面**打开备份库做 ``PRAGMA quick_check`` + 读三张表的计数，
  证明"这份备份拷回去能开"。

本文件同时是 `tests/unit/test_backup_restore.py` 的可导入模块（backup / verify / restore_smoke 三个函数）。

注意**不做界面 / 任务系统联动**：并发写入时（任务正在写 files/）可能拷到半截文件——本工具至少
在文档里提示，真正的"先停任务再备份"由调用方（定时任务 T3.4）负责。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 作为脚本运行时 sys.path[0] 是 scripts/，要往上补仓库根才能 import app
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: manifest 文件名（约定死了，verify / restore-smoke 都靠它）。
MANIFEST_NAME = "manifest.json"

#: 备份包的库文件在目录里的相对路径（data/ 布局，直接拷回 data/ 就能用）。
DB_RELPATH = "data/helper.db"


def _sha256(path: Path) -> str:
    """流式 sha256（大文件不整块读进内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _walk_files(base: Path) -> list[Path]:
    """列出 base 下全部普通文件（不含目录），排序保证 manifest 稳定。"""
    if not base.is_dir():
        return []
    return [p for p in sorted(base.rglob("*")) if p.is_file()]


def _relposix(path: Path, base: Path) -> str:
    """相对路径用正斜杠存进 manifest，Windows / POSIX 上读法一致。"""
    return path.resolve().relative_to(base.resolve()).as_posix()


def _vacuum_into(src: Path, dest: Path) -> None:
    """VACUUM INTO 出一份一致快照（只读源库）。

    ``VACUUM INTO '<目标>'`` 的 **目标必须不存在**（已存在会直接报错），所以我们只生成
    中间文件 ``<dest>.tmp``，成功后再由调用方 ``os.replace`` 成最终名。
    """
    conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        # 路径里可能有单引号，先翻倍转义，避免整条 SQL 被截断。
        conn.execute("VACUUM INTO '" + str(dest).replace("'", "''") + "'")
    finally:
        conn.close()


def default_to_dir() -> Path:
    """默认目标：``安装目录/../helper-backup/<时间戳>``。"""
    from app.core import paths

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return paths.project_root().resolve().parent / "helper-backup" / stamp


def backup(
    to_dir: str | Path,
    *,
    db_path: Path | None = None,
    files_path: Path | None = None,
    userdict_path: Path | None = None,
) -> dict:
    """把 data/ 的内容备份到 ``to_dir``，返回写进的 manifest。

    参数（默认都取自 ``app.core.paths``，测试可显式指临时目录；不给就是现役安装目录）：
        to_dir: 目标目录（不存在则建）。
        db_path: 数据库文件。
        files_path: ``data/files/`` 目录。
        userdict_path: ``data/userdict.txt``。
    """
    from app.core import paths

    to = Path(to_dir)
    to.mkdir(parents=True, exist_ok=True)
    db = db_path or paths.db_file()
    fdir = files_path if files_path is not None else paths.files_dir()

    if not db.is_file():
        raise FileNotFoundError(f"数据库不存在：{db}")

    data_out = to / "data"
    data_out.mkdir(parents=True, exist_ok=True)

    # 1) 数据库：VACUUM INTO 一致快照。目标 helper.db 不能预先存在，先写 .tmp 再替换。
    db_dest = data_out / paths.DB_FILENAME
    out_tmp = db_dest.with_name(db_dest.name + ".tmp")
    if db_dest.exists():
        # 重复备份到同一目录时不覆盖旧的，直接报错；调用方应换新目录。
        raise FileExistsError(f"目标已存在：{db_dest}")
    _vacuum_into(db, out_tmp)
    out_tmp.replace(db_dest)

    entries: list[dict] = [
        {
            "relpath": DB_RELPATH,
            "bytes": db_dest.stat().st_size,
            "sha256": _sha256(db_dest),
            "kind": "db",
        }
    ]

    # 2) data/files/：逐文件复制（含子目录），保留相对结构。
    files_out = data_out / "files"
    if fdir.is_dir():
        shutil.copytree(fdir, files_out, dirs_exist_ok=True)
    for item in _walk_files(files_out):
        entries.append(
            {
                "relpath": _relposix(item, to),
                "bytes": item.stat().st_size,
                "sha256": _sha256(item),
                "kind": "file",
            }
        )

    # 3) userdict.txt 等小文件（有就带，没有就跳过）。
    udict = userdict_path if userdict_path is not None else paths.data_dir() / "userdict.txt"
    if udict.is_file():
        dest = data_out / udict.name
        shutil.copyfile(udict, dest)
        entries.append(
            {
                "relpath": _relposix(dest, to),
                "bytes": dest.stat().st_size,
                "sha256": _sha256(dest),
                "kind": "file",
            }
        )

    manifest = {
        "tool": "backup_data.py",
        "spec_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": str(paths.project_root()),
        "source_db": str(db),
        "entries": entries,
    }
    _write_json(to / MANIFEST_NAME, manifest)
    return manifest


def verify(backup_dir: str | Path) -> tuple[bool, list[str]]:
    """重算备份目录里的文件并与 manifest 比对。

    返回 ``(是否一致, 问题清单)``。任何一项大小 / 哈希不符或缺失都算不一致。
    """
    to = Path(backup_dir)
    manifest = _read_json(to / MANIFEST_NAME)
    problems: list[str] = []
    for entry in manifest["entries"]:
        rel = entry["relpath"]
        path = to / rel
        if not path.is_file():
            problems.append(f"缺失: {rel}")
            continue
        size = path.stat().st_size
        if size != entry["bytes"]:
            problems.append(f"大小不符: {rel} (期望 {entry['bytes']}，实际 {size})")
            continue
        if _sha256(path) != entry["sha256"]:
            problems.append(f"哈希不符: {rel}")
    return (not problems), problems


def restore_smoke(backup_dir: str | Path) -> dict:
    """在工具层面打开备份库验证可恢复性。

    返回 ``{quick_check, documents, chunks, jobs}``。这个"能开"的证明不依赖用户有没有 dev 环境。
    """
    to = Path(backup_dir)
    db = to / DB_RELPATH
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        counts = {
            "documents": int(conn.execute("SELECT count(*) FROM documents").fetchone()[0]),
            "chunks": int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0]),
            "jobs": int(conn.execute("SELECT count(*) FROM jobs").fetchone()[0]),
        }
    finally:
        conn.close()
    return {"quick_check": quick, **counts}


def _summary(manifest: dict) -> tuple[int, int]:
    total = sum(e["bytes"] for e in manifest["entries"])
    return len(manifest["entries"]), total


def main(argv: list[str] | None = None) -> int:
    if sys.stdout is not None:
        with contextlib.suppress(AttributeError, ValueError, OSError):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="数据备份 + 恢复验证（T3.3）")
    parser.add_argument("--to", metavar="目录", help="做备份到该目录（默认 ..\\helper-backup\\<时间戳>）")
    parser.add_argument("--verify", metavar="目录", help="校验备份，不一致退出码 1")
    parser.add_argument("--restore-smoke", metavar="目录", help="打开备份库做 quick_check + 计数")
    args = parser.parse_args(argv)

    if args.verify:
        ok, problems = verify(args.verify)
        print(f"manifest: {Path(args.verify) / MANIFEST_NAME}")
        if ok:
            print("校验通过（大小/sha256 全部一致）")
            return 0
        print("校验失败：")
        for line in problems:
            print(f"  - {line}")
        return 1

    if args.restore_smoke:
        try:
            info = restore_smoke(args.restore_smoke)
        except sqlite3.Error as exc:
            print(f"restore-smoke 失败（库打不开）：{exc}")
            return 1
        print(f"quick_check = {info['quick_check']}")
        for key in ("documents", "chunks", "jobs"):
            print(f"{key} = {info[key]}")
        return 0 if info["quick_check"] == "ok" else 1

    dest = Path(args.to) if args.to else default_to_dir()
    manifest = backup(dest)
    count, total = _summary(manifest)
    print(f"备份完成：{dest}")
    print(f"条目 {count} 个，共 {total} 字节")
    print(f"manifest: {dest / MANIFEST_NAME}")
    print("建议接着跑：--verify 和 --restore-smoke。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
