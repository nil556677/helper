"""v1 知识库事实来源：documents / chunks 的唯一 SQL 入口。

- parent 与 child 同表，`is_parent` 区分，child 用 `parent_id` 回填；
- `version + status` 支持"新版本先写 pending、成功再 activate、然后删旧版本"；
- 父块全文只在这里（Qdrant 不再存 parent）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from typing import Any

from app.core.db.connection import get_connection, transaction


def _now() -> float:
    return time.time()


def insert_document(
    *,
    name: str,
    stored_path: str,
    file_hash: str,
    text_hash: str = "",
    size: int = 0,
    source_path: str = "",
    status: str = "pending",
    doc_title: str = "",
    version: int = 1,
) -> int:
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO documents(name, source_path, stored_path, file_hash, text_hash,"
            " size, doc_title, version, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                source_path,
                stored_path,
                file_hash,
                text_hash,
                int(size),
                doc_title,
                int(version),
                status,
                _now(),
                _now(),
            ),
        )
        return int(cursor.lastrowid)


def find_by_file_hash(file_hash: str) -> Any | None:
    return get_connection().execute(
        "SELECT * FROM documents WHERE file_hash = ?", (file_hash,)
    ).fetchone()


def find_by_text_hash(text_hash: str) -> Any | None:
    return get_connection().execute(
        "SELECT * FROM documents WHERE text_hash = ?", (text_hash,)
    ).fetchone()


def get_document(doc_id: int) -> Any | None:
    return get_connection().execute(
        "SELECT * FROM documents WHERE id = ?", (int(doc_id),)
    ).fetchone()


def documents_by_ids(doc_ids: Iterable[int]) -> dict[int, dict]:
    """按主键批量取文档行（证据溯源要 text_hash，避免 N+1）。"""
    ids = sorted({int(value) for value in doc_ids if value is not None})  # 去重排序
    if not ids:  # 空入参
        return {}  # 直接返回
    placeholders = ", ".join("?" for _ in ids)  # 占位符
    rows = get_connection().execute(  # 一次查完
        f"SELECT * FROM documents WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}  # id -> 行


def update_document(doc_id: int, **fields: object) -> None:
    allowed = {
        key: value
        for key, value in fields.items()
        if key in {
            "name", "stored_path", "file_hash", "doc_title", "text_hash", "size",
            "version", "status", "error_kind", "error", "pages",
        }
    }
    if not allowed:
        return
    sets = ", ".join(f"{key} = ?" for key in allowed)
    with transaction() as conn:
        conn.execute(
            f"UPDATE documents SET {sets}, updated_at = ? WHERE id = ?",
            [*allowed.values(), _now(), int(doc_id)],
        )


def mark_failed(doc_id: int, *, kind: str, detail: str) -> None:
    update_document(doc_id, status="failed", error_kind=kind, error=detail)


def insert_parent(
    conn: Any,
    *,
    doc_id: int,
    version: int,
    text: str,
    heading: str = "",
    section: Sequence[str] = (),
    loc: dict | None = None,
    token_count: int = 0,
    kind: str = "text",
    role: str = "section_parent",
    fetch_mode: int = 0,
    table_uid: str | None = None,
    row_uid: str | None = None,
    row_no: int | None = None,
    segment_index: int | None = None,
    segment_count: int = 1,
    merged_ranges: Sequence[Sequence[int]] = (),
) -> int:
    cursor = conn.execute(
        "INSERT INTO chunks(doc_id, version, parent_id, is_parent, seq, kind, status,"
        " text, heading, section_json, loc_json, token_count, role, fetch_mode,"
        " table_uid, row_uid, row_no, segment_index, segment_count, merged_json, created_at)"
        " VALUES (?, ?, NULL, 1, 0, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(doc_id),
            int(version),
            kind,
            text,
            heading,
            json.dumps(list(section), ensure_ascii=False),
            json.dumps(loc or {}, ensure_ascii=False),
            int(token_count),
            role,
            int(fetch_mode),
            table_uid,
            row_uid,
            row_no,
            segment_index,
            int(segment_count),
            json.dumps([list(item) for item in merged_ranges], ensure_ascii=False),
            _now(),
        ),
    )
    return int(cursor.lastrowid)


def insert_child(
    conn: Any,
    *,
    doc_id: int,
    version: int,
    parent_id: int,
    seq: int,
    text: str,
    heading: str = "",
    section: Sequence[str] = (),
    loc: dict | None = None,
    token_count: int = 0,
    kind: str = "text",
    hard_split: bool = False,
    role: str = "text",
    fetch_mode: int = 1,
    table_uid: str | None = None,
    row_uid: str | None = None,
    row_no: int | None = None,
    segment_index: int | None = None,
    segment_count: int = 1,
    merged_ranges: Sequence[Sequence[int]] = (),
) -> int:
    cursor = conn.execute(
        "INSERT INTO chunks(doc_id, version, parent_id, is_parent, seq, kind, status,"
        " text, heading, section_json, loc_json, token_count, hard_split, role,"
        " fetch_mode, table_uid, row_uid, row_no, segment_index, segment_count,"
        " merged_json, created_at)"
        " VALUES (?, ?, ?, 0, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(doc_id),
            int(version),
            int(parent_id),
            int(seq),
            kind,
            text,
            heading,
            json.dumps(list(section), ensure_ascii=False),
            json.dumps(loc or {}, ensure_ascii=False),
            int(token_count),
            1 if hard_split else 0,
            role,
            int(fetch_mode),
            table_uid,
            row_uid,
            row_no,
            segment_index,
            int(segment_count),
            json.dumps([list(item) for item in merged_ranges], ensure_ascii=False),
            _now(),
        ),
    )
    return int(cursor.lastrowid)


def activate_version(doc_id: int, version: int, *, pages: int | None = None) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE chunks SET status = 'active' WHERE doc_id = ? AND version = ?",
            (int(doc_id), int(version)),
        )
        conn.execute(
            "UPDATE documents SET status = 'indexed', version = ?, pages = COALESCE(?, pages),"
            " updated_at = ? WHERE id = ?",
            (int(version), pages, _now(), int(doc_id)),
        )


def delete_version(doc_id: int, version: int) -> int:
    with transaction() as conn:
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE doc_id = ? AND version = ?",
                (int(doc_id), int(version)),
            ).fetchone()[0]
        )
        conn.execute(
            "DELETE FROM chunks WHERE doc_id = ? AND version = ?",
            (int(doc_id), int(version)),
        )
        return total


def delete_document(doc_id: int) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (int(doc_id),))


def parents_for(parent_ids: Iterable[int]) -> dict[int, dict]:
    ids = sorted({int(value) for value in parent_ids if value is not None})
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = get_connection().execute(
        f"SELECT * FROM chunks WHERE is_parent = 1 AND id IN ({placeholders})", ids
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def siblings_for(
    doc_id: int, version: int, table_uid: str, row_uid: str
) -> list[dict]:
    """同一行的全部列组段（`fetch_mode=2`），严格按 `segment_index` 升序。"""
    if not table_uid or not row_uid:
        return []
    rows = get_connection().execute(
        "SELECT * FROM chunks WHERE doc_id = ? AND version = ?"
        " AND table_uid = ? AND row_uid = ? AND fetch_mode = 2"
        " AND is_parent = 0 AND status = 'active' ORDER BY segment_index",
        (int(doc_id), int(version), str(table_uid), str(row_uid)),
    ).fetchall()
    return [dict(row) for row in rows]


def active_versions(doc_ids: Iterable[int]) -> dict[int, int]:
    """当前可检索的文档版本（用于丢弃旧版本 Qdrant 点）。"""
    ids = sorted({int(value) for value in doc_ids if value is not None})
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = get_connection().execute(
        f"SELECT id, version FROM documents WHERE status = 'indexed' AND id IN ({placeholders})",
        ids,
    ).fetchall()
    return {int(row["id"]): int(row["version"]) for row in rows}


def chunks_by_ids(chunk_ids: Iterable[int]) -> dict[int, dict]:
    """按主键批量取 active chunk（用于取原文，避免 N+1）。"""
    ids = sorted({int(value) for value in chunk_ids if value is not None})
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = get_connection().execute(
        f"SELECT * FROM chunks WHERE status = 'active' AND id IN ({placeholders})", ids
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def rows_for(
    keys: Iterable[tuple[int, int, str, str]],
) -> dict[tuple[int, int, str, str], list[dict]]:
    """批量取多行的全部列组段（`fetch_mode=2`），按 row 分组、段内有序。"""
    unique = sorted({(int(d), int(v), str(t), str(r)) for d, v, t, r in keys if t and r})
    if not unique:
        return {}
    clauses = " OR ".join(
        "(doc_id = ? AND version = ? AND table_uid = ? AND row_uid = ?)"
        for _ in unique
    )
    params: list[object] = []
    for key in unique:
        params.extend(key)
    rows = get_connection().execute(
        "SELECT * FROM chunks WHERE status = 'active' AND fetch_mode = 2"
        f" AND is_parent = 0 AND ({clauses}) ORDER BY segment_index",
        params,
    ).fetchall()
    grouped: dict[tuple[int, int, str, str], list[dict]] = {}
    for row in rows:
        item = dict(row)
        key = (
            int(item["doc_id"]),
            int(item["version"]),
            str(item["table_uid"]),
            str(item["row_uid"]),
        )
        grouped.setdefault(key, []).append(item)
    return grouped
