"""净室 v2 的一次性重置脚本。

做什么（顺序固定）：

1. 单实例守卫：应用在跑就拒绝；
2. 备份旧 ``data/helper.db`` → ``data/backup/helper-<ts>.db``；
3. 导出旧 ``llm_slots``（默认保留，测试需要模型配置）；
4. 删除旧库文件（helper.db / -wal / -shm）与 ``data/langgraph/``；
5. 重置 Qdrant（**只服务端**）：能连上就按集合删；否则清掉
   ``data/qdrant-server/{collections,aliases}``（本地嵌入式 ``data/qdrant`` 已废弃）；
6. 建 v1 schema，写回保留的 ``llm_slots``；
7. 重新导入 ``data/files/``（阶段 2 接好新入库链路后启用 ``--reimport``）。

用法：
    python scripts/reset_workspace.py --dry-run        # 只打印将做什么
    python scripts/reset_workspace.py --no-keep-llm-slots
    python scripts/reset_workspace.py --reimport       # 阶段 2 后可用
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import paths
from app.core.db import schema_v1

#: 已知的旧/新集合名，重置时全部删除（幂等）。
_KNOWN_COLLECTIONS = (
    "rag_chunks",
    "rag_chunks_v2",
    "rag_parents",
    "kb_chunks",
)


def _old_db_settings(conn: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for key in ("vector.url", "vector.api_key"):
            row = conn.execute(
                "SELECT value_json FROM settings WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                out[key] = str(row[0])
    except sqlite3.Error:
        pass
    return out


def _export_llm_slots(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute("SELECT * FROM llm_slots").fetchall()
    except sqlite3.Error:
        return []
    columns = [
        column[1]
        for column in conn.execute("PRAGMA table_info(llm_slots)").fetchall()
    ]
    return [dict(zip(columns, row, strict=False)) for row in rows]


def _reset_qdrant_storage(dry_run: bool, settings: dict[str, str]) -> list[str]:
    """删除 Qdrant 里的旧集合。返回做过的动作。"""
    actions: list[str] = []
    # 1) 服务端可达 → 用官方 client 删集合（最干净）
    url = settings.get("vector.url", "")
    api_key = settings.get("vector.api_key", "")
    if url.startswith("http"):
        try:
            from qdrant_client import QdrantClient

            kwargs: dict[str, object] = {"url": url, "timeout": 3}
            if api_key and api_key.startswith("enc:"):
                # 密文解不开就不带 key；本机服务端一般不校验
                kwargs.pop("api_key", None)
            client = QdrantClient(**kwargs)
            existing = [c.name for c in client.get_collections().collections]
            for name in _KNOWN_COLLECTIONS:
                if name in existing:
                    if dry_run:
                        actions.append(f"[dry-run] 删除 Qdrant 集合 {name}（{url}）")
                    else:
                        client.delete_collection(name)
                        actions.append(f"已删除 Qdrant 集合 {name}")
        except Exception as exc:
            actions.append(f"Qdrant 服务端不可达（{exc}），改为清本地存储目录")
    # 2) 无论如何，清掉本机存储目录里的集合数据（应用已停止时最可靠）
    for rel in ("qdrant-server/collections", "qdrant-server/aliases"):
        target = paths.data_dir() / rel
        if target.exists():
            if dry_run:
                actions.append(f"[dry-run] 删除 {rel}/")
            else:
                shutil.rmtree(target, ignore_errors=True)
                actions.append(f"已删除 {rel}/")
    return actions


def _ensure_backup_dir() -> Path:
    backup = paths.data_dir() / "backup"
    backup.mkdir(parents=True, exist_ok=True)
    return backup


def _reimport_files(importer=None) -> None:
    """从 data/files/ 逐份重导；importer 可注入（测试）。"""
    files = sorted(
        path for path in paths.files_dir().glob("*/*") if path.is_file()
    )
    if not files:
        print("data/files/ 下没有可重导的文件。")
        return
    print(f"开始重导 {len(files)} 份资料…")
    for path in files:
        if importer is None:
            from app.ingest import import_stored

            outcome = import_stored(path, name=path.name)
        else:
            outcome = importer(path, name=path.name)
        status = getattr(outcome, "status", "?")
        reason = getattr(outcome, "reason", "")
        extra = f"（{reason}）" if reason else ""
        print(f"  - {path.name}：{status}{extra}")


def run(
    *,
    dry_run: bool,
    keep_llm_slots: bool,
    reimport: bool,
    importer=None,
) -> int:
    from app.core import single_instance

    if not single_instance.acquire():
        print("另一个 helper 实例正在运行，请先关闭应用再重置。")
        return 1

    db_file = paths.db_file()
    data_root = paths.data_dir()

    steps: list[str] = []

    # 1) 备份
    if db_file.is_file():
        backup = _ensure_backup_dir() / f"helper-pre-v2-{int(time.time())}.db"
        steps.append(f"备份 {db_file.name} → {backup.relative_to(data_root)}")

    # 2) 读旧库（备份前先导出 llm_slots / vector 设置）
    slots: list[dict] = []
    settings: dict[str, str] = {}
    if db_file.is_file():
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        with contextlib.suppress(sqlite3.Error):
            settings = _old_db_settings(conn)
            if keep_llm_slots:
                slots = _export_llm_slots(conn)
        conn.close()
        steps.append(f"导出 llm_slots {len(slots)} 行")

    # 3) 删除旧库与检查点
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(db_file) + suffix)
        if target.exists():
            steps.append(f"删除 {target.name}")
    langgraph = data_root / "langgraph"
    if langgraph.exists():
        steps.append("删除 data/langgraph/")

    # 4) 重置 Qdrant
    qdrant_actions = _reset_qdrant_storage(dry_run, settings)
    steps.extend(qdrant_actions)

    # 5) 建 v1 schema
    steps.append("建 v1 schema（SCHEMA_VERSION=1）")
    if keep_llm_slots and slots:
        steps.append(f"写回 llm_slots {len(slots)} 行")

    # 6) 重导
    if reimport:
        files = sorted(
            path for path in paths.files_dir().glob("*/*") if path.is_file()
        )
        steps.append(f"重新导入 data/files/ 下 {len(files)} 份资料")

    print("== 重置计划 ==")
    for step in steps:
        print("  -", step)
    print()

    if dry_run:
        print("dry-run：未做任何改动。")
        return 0

    # ---- 真正执行 ----
    if db_file.is_file():
        backup = _ensure_backup_dir() / f"helper-pre-v2-{int(time.time())}.db"
        shutil.copy2(db_file, backup)
        print(f"已备份 → {backup}")
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(db_file) + suffix)
        if target.exists():
            target.unlink(missing_ok=True)
    if langgraph.exists():
        shutil.rmtree(langgraph, ignore_errors=True)

    _reset_qdrant_storage(False, settings)

    conn = sqlite3.connect(str(db_file))
    try:
        version = schema_v1.initialize(conn)
        conn.commit()
    finally:
        conn.close()
    print(f"已建 v1 schema（version={version}）")

    if keep_llm_slots and slots:
        conn = sqlite3.connect(str(db_file))
        try:
            for row in slots:
                columns = list(row.keys())
                placeholders = ", ".join("?" for _ in columns)
                conn.execute(
                    f"INSERT OR REPLACE INTO llm_slots({', '.join(columns)})"
                    f" VALUES ({placeholders})",
                    tuple(row[column] for column in columns),
                )
            conn.commit()
            print(f"已写回 llm_slots {len(slots)} 行")
        finally:
            conn.close()

    # 保留 Qdrant 服务端地址（否则重置后要重新配端口）
    vector_settings = {
        key: value for key, value in settings.items() if key in ("vector.url", "vector.api_key")
    }
    if vector_settings:
        import json

        conn = sqlite3.connect(str(db_file))
        try:
            for key, raw in vector_settings.items():
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = raw
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value_json, updated_at)"
                    " VALUES (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), time.time()),
                )
            conn.commit()
            print(f"已保留 Qdrant 服务端设置 {len(vector_settings)} 项")
        finally:
            conn.close()

    if reimport:
        _reimport_files(importer)

    print("重置完成。旧数据已清除；llm_slots 已保留（如选择）；资料在 data/files/ 未动。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="净室 v2 一次性重置")
    parser.add_argument("--dry-run", action="store_true", help="只打印将做什么")
    parser.add_argument(
        "--keep-llm-slots",
        dest="keep_llm_slots",
        action="store_true",
        default=True,
        help="保留 llm_slots（默认）",
    )
    parser.add_argument(
        "--no-keep-llm-slots",
        dest="keep_llm_slots",
        action="store_false",
        help="不保留 llm_slots（彻底清空）",
    )
    parser.add_argument("--reimport", action="store_true", help="重置后重新导入 data/files/")
    args = parser.parse_args(argv)
    return run(
        dry_run=args.dry_run,
        keep_llm_slots=args.keep_llm_slots,
        reimport=args.reimport,
    )


if __name__ == "__main__":
    raise SystemExit(main())
