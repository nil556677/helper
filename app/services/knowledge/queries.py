"""知识库查询（v1 / SQLite 事实来源）：列表/统计/详情/片段/预览。

对 UI 保持原有服务 API；数据全部来自 `documents` / `chunks`，不再查 Qdrant。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.db.connection import get_connection
from app.rag import kb_store

from ._shared import DocumentPage, DocumentView, KnowledgeStats

PREVIEW_CHARS = 2000
PREVIEW_PDF_PAGES = 3

_SORTS = {
    "name": "d.name COLLATE NOCASE",
    "size": "d.size",
    "chunks": "chunks",
    "status": "d.status",
    "updated_at": "d.updated_at",
}

_LIST_SELECT = """
    SELECT d.id, d.name, d.size, d.status, d.error AS detail, d.updated_at,
           d.pages, NULL AS ocr_pages, NULL AS chars,
           (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id AND c.is_parent = 0) AS chunks
    FROM documents d
"""


def stats() -> KnowledgeStats:
    conn = get_connection()
    documents = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    failed = int(
        conn.execute("SELECT COUNT(*) FROM documents WHERE status = 'failed'").fetchone()[0]
    )
    indexing = int(
        conn.execute("SELECT COUNT(*) FROM documents WHERE status = 'pending'").fetchone()[0]
    )
    chunks = int(conn.execute("SELECT COUNT(*) FROM chunks WHERE is_parent = 0").fetchone()[0])
    active = int(
        conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE is_parent = 0 AND status = 'active'"
        ).fetchone()[0]
    )
    row = conn.execute(
        "SELECT COALESCE(SUM(size), 0) AS bytes, MAX(updated_at) AS updated_at FROM documents"
    ).fetchone()
    ratio = int(active / chunks * 100) if chunks else 100
    return KnowledgeStats(
        documents=documents,
        failed=failed,
        chunks=chunks,
        bytes=int(row["bytes"] or 0),
        updated_at=row["updated_at"],
        indexing=indexing,
        vector_bytes=0,
        vectors_done=active,
        vectors_total=chunks,
        vector_ratio=ratio,
        vector_error="",
    )


def documents() -> tuple[DocumentView, ...]:
    rows = get_connection().execute(_LIST_SELECT + " ORDER BY d.updated_at DESC").fetchall()
    return tuple(DocumentView.from_row(row) for row in rows)


def _where(query: str, status: str | None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if query.strip():
        clauses.append("d.name LIKE ? ESCAPE '\\'")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    if status:
        clauses.append("d.status = ?")
        params.append(status)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def documents_page(
    *,
    page: int = 1,
    size: int = 20,
    query: str = "",
    status: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
) -> DocumentPage:
    size = max(1, int(size))
    where, params = _where(query, status)
    conn = get_connection()
    total = int(conn.execute(f"SELECT COUNT(*) FROM documents d {where}", params).fetchone()[0])
    pages = max(1, -(-total // size))
    current = min(max(1, int(page)), pages)
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
    sort_sql = _SORTS.get(sort, _SORTS["updated_at"])
    rows = conn.execute(
        _LIST_SELECT + f" {where} ORDER BY {sort_sql} {order_sql} LIMIT ? OFFSET ?",
        [*params, size, (current - 1) * size],
    ).fetchall()
    return DocumentPage(
        items=tuple(DocumentView.from_row(row) for row in rows),
        total=total,
        page=current,
        pages=pages,
        size=size,
    )


def _parse_json(raw: object, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (ValueError, TypeError):
        return default


def _section_path_list(raw: object) -> list[str]:
    data = _parse_json(raw, [])
    return [str(item) for item in data] if isinstance(data, list) else []


def _count(doc_id: int, *, parent: bool) -> int:
    return int(
        get_connection().execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id = ? AND is_parent = ?",
            (int(doc_id), 1 if parent else 0),
        ).fetchone()[0]
    )


def document_detail(doc_id: int) -> dict[str, object] | None:
    row = kb_store.get_document(int(doc_id))
    if row is None:
        return None
    children = int(
        get_connection().execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id = ? AND is_parent = 0",
            (int(doc_id),),
        ).fetchone()[0]
    )
    parents = _count(int(doc_id), parent=True)
    active = int(
        get_connection().execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id = ? AND is_parent = 0 AND status = 'active'",
            (int(doc_id),),
        ).fetchone()[0]
    )
    return {
        "id": int(doc_id),
        "name": str(row["name"]),
        "doc_title": str(row["doc_title"] or ""),
        "status": str(row["status"]),
        "detail": str(row["error"] or ""),
        "size": int(row["size"] or 0),
        "pages": None if row["pages"] is None else int(row["pages"]),
        "ocr_pages": (),
        "chars": None,
        "children": children,
        "parents": parents,
        "vectors_done": active,
        "vectors_total": children,
        "version": int(row["version"] or 1),
        "doc_key": str(row["file_hash"] or ""),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "versions": [
            {
                "id": int(doc_id),
                "version": int(row["version"] or 1),
                "status": str(row["status"]),
                "updated_at": float(row["updated_at"]),
                "superseded_at": None,
            }
        ],
        "code_kinds": (),
    }


def document_codes(doc_id: int, *, kind: str | None = None, limit: int = 300) -> list[dict[str, object]]:
    del doc_id, kind, limit
    return []


def _chunk_item(row: Any, *, parent: Any | None) -> dict[str, object]:
    from app.core.chunk.index_text import index_text

    text = str(row["text"] or "")
    path = _section_path_list(row["section_json"])
    loc = _parse_json(row["loc_json"], {})
    heading = str(row["heading"] or " › ".join(path))
    return {
        "seq": int(row["seq"] or 0),
        "page": loc.get("page"),
        "section": path[-1] if path else "",
        "kind": str(row["kind"] or "text"),
        "preview": " ".join(text.split())[:60],
        "key": f"{row['doc_id']}:{row['version']}:{row['id']}",
        "section_path": path,
        "section_kind": "section" if path else "front_matter",
        "heading": heading,
        "loc": loc,
        "level": None,
        "parent_key": str(row["parent_id"] or ""),
        "parent_heading": str(parent["heading"] or "") if parent else "",
        "parent_text": str(parent["text"] or "") if parent else "",
        "chars": len(text),
        "content_hash": "",
        "policy_version": None,
        "embedding_version": None,
        "text": text,
        "index_text": index_text(text, path),
    }


def document_chunks(
    doc_id: int,
    *,
    page: int = 1,
    size: int = 50,
    kind: str | None = None,
    heading: str | None = None,
) -> dict[str, object]:
    row = kb_store.get_document(int(doc_id))
    if row is None:
        return {"items": [], "total": 0, "page": 1, "pages": 1, "kinds": [], "error": "资料不存在"}
    version = int(row["version"] or 1)
    sql = "SELECT * FROM chunks WHERE doc_id = ? AND version = ? AND is_parent = 0"
    params: list[Any] = [int(doc_id), version]
    if kind:
        sql += " AND kind = ?"
        params.append(str(kind))
    sql += " ORDER BY seq"
    rows = [dict(item) for item in get_connection().execute(sql, params).fetchall()]
    if heading and heading.strip():
        needle = heading.strip().lower()
        rows = [item for item in rows if needle in str(item.get("heading") or "").lower()]
    total = len(rows)
    size = max(1, int(size))
    pages = max(1, -(-total // size))
    current = min(max(1, int(page)), pages)
    window = rows[(current - 1) * size : current * size]
    parent_ids = [item["parent_id"] for item in window if item.get("parent_id")]
    parents = kb_store.parents_for(parent_ids)
    items = [
        _chunk_item(item, parent=parents.get(int(item["parent_id"])) if item.get("parent_id") else None)
        for item in window
    ]
    kinds = sorted({str(item.get("kind") or "text") for item in rows})
    return {"items": items, "total": total, "page": current, "pages": pages, "kinds": kinds}


def document_preview(doc_id: int, *, limit: int = PREVIEW_CHARS) -> dict[str, object]:
    from app.core import paths

    row = kb_store.get_document(int(doc_id))
    if row is None:
        return {"found": False, "name": "", "text": "", "truncated": False, "note": "这份资料不在库里了"}
    name = str(row["name"])
    stored = str(row["stored_path"] or "")
    copy = paths.resolve_stored_path(stored) if stored else None
    if copy is None or not copy.is_file():
        return {
            "found": True, "name": name, "text": "", "truncated": False,
            "note": "副本不在了（可能被清理过），只能看片段",
        }
    suffix = copy.suffix.lower()
    if suffix == ".pdf":
        try:
            import fitz

            document = fitz.open(str(copy))
            parts = [
                document.load_page(index).get_text()
                for index in range(min(PREVIEW_PDF_PAGES, document.page_count))
            ]
            document.close()
            text = "\n".join(parts)
        except Exception as exc:
            return {"found": True, "name": name, "text": "", "truncated": False, "note": f"打不开副本：{exc}"}
    elif suffix in (".txt", ".md", ".markdown", ".csv", ".json", ".log", ".html", ".htm") or not suffix:
        try:
            with copy.open("rb") as handle:
                raw = handle.read(limit * 4 + 3)
        except OSError as exc:
            return {"found": True, "name": name, "text": "", "truncated": False, "note": f"读不了副本：{exc}"}
        text = raw.decode("utf-8", errors="replace")
    else:
        return {
            "found": True, "name": name, "text": "", "truncated": False,
            "note": f"{suffix} 暂不支持在内置右栏预览（导出后可用原程序打开）",
        }
    truncated = len(text) > limit
    return {
        "found": True, "name": name, "text": text[:limit], "truncated": truncated,
        "note": f"只显示开头 {limit} 字" if truncated else "",
    }
