"""知识库的查询/列表/统计/详情/预览/分块/精确字段。"""

from __future__ import annotations

import json
from dataclasses import replace

from loguru import logger

from app.core.vector import api as vector_api
from app.rag import store

from ._shared import DocumentPage, DocumentView, KnowledgeStats

#: 预览时最多读多少字符（右栏是"核对一下是哪一份"，不是阅读器）
PREVIEW_CHARS = 2000
#: PDF 预览最多读几页（大文件只抽开头，别让右栏卡住）
PREVIEW_PDF_PAGES = 3


def _safe_ocr_pages(raw: object) -> tuple[int, ...]:
    """解析 ``documents.ocr_pages``（JSON），库被外部写坏时降级为空，不崩详情页。"""
    if not raw:
        return ()
    try:
        return tuple(int(item) for item in json.loads(str(raw)))
    except (TypeError, ValueError):
        return ()


def stats() -> KnowledgeStats:
    """概览：就绪份数 / 片段数 / 副本占用 / 最后更新（SQL 在 ingest store 里）。

    **向量后端不可用时降级**（不抛）：覆盖率/向量占用置 0 并在 ``vector_error`` 里带原因，
    免得界面自动刷新每 2 秒抛一次 500（实测曾刷出 971KB traceback）。
    """
    row = store.counts()
    coverage: dict[str, int] = {"done": 0, "total": 0, "ratio": 100}
    vector_bytes = 0
    vector_error = ""
    try:
        coverage = store.vector_coverage()
        vector_bytes = store.vector_bytes()
    except Exception as exc:  # Qdrant 锁被占/服务端连不上/集合缺失
        vector_error = str(exc) or type(exc).__name__
        logger.warning("概览的向量统计暂不可用：{}", vector_error)
    return KnowledgeStats(
        documents=int(row["documents"]),
        failed=int(row["failed"]),
        chunks=int(row["chunks"]),
        bytes=int(row["bytes"]),
        updated_at=row["updated_at"],
        indexing=int(row["indexing"]),
        vector_bytes=vector_bytes,
        vectors_done=int(coverage["done"]),
        vectors_total=int(coverage["total"]),
        vector_ratio=int(coverage["ratio"]),
        vector_error=vector_error,
    )


def documents() -> tuple[DocumentView, ...]:
    """全部资料（新的在前）。**界面不用它**（列表走 :func:`documents_page` 分页），
    留着是给测试与"导出/统计"这类需要全量的地方用。"""
    return tuple(DocumentView.from_row(row) for row in store.list_all())


def documents_page(
    *,
    page: int = 1,
    size: int = 20,
    query: str = "",
    status: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
) -> DocumentPage:
    """一页资料（**服务端分页**：`LIMIT/OFFSET` + 一条 `count(*)`）。

    为什么必须服务端分页：资料可能几千份，"全查出来再切页"又慢又占内存，
    而且总数一多，界面光画 DOM 就得几百毫秒。

    参数：
        page: 页码（1 基；越界会被收回合法范围）。
        size: 每页条数。
        query: 名称筛选（子串匹配，空表示不筛）。
    """
    total = store.count(query, status=status)
    size = max(1, int(size))
    pages = max(1, -(-total // size))
    current = min(max(1, int(page)), pages)
    rows = store.page(
        query, size=size, offset=(current - 1) * size, status=status, sort=sort, order=order
    )
    items = [DocumentView.from_row(row) for row in rows]
    # **行内向量化进度**（P1.11）：一页**只查一次**（按 payload.doc_id 分组），
    # 绝不在 from_row 里逐行查——那一行代码等于每行扫一遍 vector_keys 表。
    processing = [item.id for item in items if item.status == "indexing"]
    if processing:
        progress = store.vector_progress_by_doc(processing)
        items = [
            (
                replace(
                    item,
                    vectors_done=progress.get(item.id, (0, 0))[0],
                    vectors_total=progress.get(item.id, (0, 0))[1],
                )
                if item.status == "indexing"
                else item
            )
            for item in items
        ]
    return DocumentPage(
        items=tuple(items),
        total=total,
        page=current,
        pages=pages,
        size=size,
    )


def document_detail(doc_id: int) -> dict[str, object] | None:
    """**资料详情**（详情页的总览部分）：一份资料的全部展示数据，一次查完。

    返回 ``None`` 表示这份资料不在了。字段：
        ``id / name / status / detail / size / pages / ocr_pages / chars /
        children / parents / vectors_done / vectors_total / version / doc_key /
        created_at / updated_at / versions / code_kinds``
    """
    row = store.get(int(doc_id))
    if row is None:
        return None
    style = str(row["status"])
    # Qdrant 相关字段单独守卫：后端不可用时降级为空值 + 标记，不让单个字段崩掉详情页。
    progress = (0, 0)
    children = 0
    parents = 0
    try:
        progress = store.vector_progress_by_doc([int(doc_id)])[int(doc_id)]
        children = _count_doc_points(vector_api.COLLECTION_CHUNKS, int(doc_id))
        parents = _count_doc_points(vector_api.COLLECTION_PARENTS, int(doc_id))
    except Exception as exc:
        logger.warning("读向量统计失败（doc {}）：{}", doc_id, exc)
    versions = [
        {
            "id": int(item["id"]),
            "version": int(item["version"] or 1),
            "status": str(item["status"]),
            "updated_at": float(item["updated_at"]),
            "superseded_at": item["superseded_at"],
        }
        for item in store.versions_of(str(row["doc_key"] or ""))
    ]
    columns = tuple(row.keys())
    return {
        "id": int(doc_id),
        "name": str(row["source_name"]),
        # 文档大标题（第一个 1 级标题）：单独存，不再进 section_path
        "doc_title": str(row["doc_title"] or "") if "doc_title" in columns else "",
        "status": style,
        "detail": str(row["detail"] or ""),
        "size": int(row["size"] or 0),
        "pages": None if row["pages"] is None else int(row["pages"]),
        "ocr_pages": _safe_ocr_pages(row["ocr_pages"]),
        "chars": None if row["chars"] is None else int(row["chars"]),
        "children": int(children),
        "parents": int(parents),
        "vectors_done": progress[0],
        "vectors_total": progress[1],
        "version": int(row["version"] or 1),
        "doc_key": str(row["doc_key"] or ""),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "versions": versions,
        "code_kinds": store.code_kinds(int(doc_id)),
    }


def document_codes(doc_id: int, *, kind: str | None = None, limit: int = 300) -> list[dict[str, object]]:
    """一份资料的**精确字段**（详情页的"编号/日期/金额/条款号"卡）。"""
    return [
        {
            "id": int(row["id"]),
            "kind": str(row["kind"]),
            "value": str(row["value"]),
            "raw": str(row["raw"] or ""),
            "page": row["page"],
        }
        for row in store.codes_for_document(int(doc_id), kind=kind, limit=limit)
    ]

def document_preview(doc_id: int, *, limit: int = PREVIEW_CHARS) -> dict[str, object]:
    """右栏"看原件"（P1.18）：读**副本**的开头一段，返回能不能看、截没截断。

    参数：
        doc_id: 资料 id。
        limit: 最多返回多少字符。
    返回：``{"found", "name", "text", "truncated", "note"}``。

    **为什么只读开头**：右栏的用途是"核对导进来的是哪一份"，不是阅读器；
    整份读进来会让大 PDF/大 xlsx 把界面卡住。
    """
    from app.core import paths

    row = store.get(doc_id)
    if row is None:
        return {"found": False, "name": "", "text": "", "truncated": False, "note": "这份资料不在库里了"}
    name = str(row["source_name"])
    stored = str(row["stored_path"] or "")
    copy = paths.resolve_stored_path(stored) if stored else None
    if copy is None or not copy.is_file():
        return {
            "found": True,
            "name": name,
            "text": "",
            "truncated": False,
            "note": "副本不在了（可能被清理过），只能看片段",
        }

    suffix = copy.suffix.lower()
    if suffix == ".pdf":
        try:
            import fitz

            document = fitz.open(str(copy))
            parts: list[str] = []
            for index in range(min(PREVIEW_PDF_PAGES, document.page_count)):
                parts.append(document.load_page(index).get_text())
            document.close()
            text = "\n".join(parts)
        except Exception as exc:
            return {"found": True, "name": name, "text": "", "truncated": False, "note": f"打不开副本：{exc}"}
    elif suffix in (".txt", ".md", ".markdown", ".csv", ".json", ".log", ".html", ".htm") or not suffix:
        # **有界读**（P1 审查 M3）：以前 `read_text()` 把整个文件读进内存再截断——
        # 实测 20 MB 文件峰值 38 MB；200 MB 的日志文件能把界面线程拖垮。
        # 只读"够显示"的字节（UTF-8 多字节安全：多读 3 字节余量，decode 后再按字符截断）。
        try:
            with copy.open("rb") as handle:
                raw = handle.read(limit * 4 + 3)
        except OSError as exc:
            return {"found": True, "name": name, "text": "", "truncated": False, "note": f"读不了副本：{exc}"}
        text = raw.decode("utf-8", errors="replace")
        truncated = copy.stat().st_size > len(raw)
    else:
        return {
            "found": True,
            "name": name,
            "text": "",
            "truncated": False,
            "note": f"{suffix} 暂不支持在内置右栏预览（导出后可用原程序打开）",
        }

    truncated = len(text) > limit
    return {
        "found": True,
        "name": name,
        "text": text[:limit],
        "truncated": truncated,
        "note": f"只显示开头 {limit} 字" if truncated else "",
    }


def _doc_child_points(doc_id: int) -> list[object]:
    """一份资料在 Qdrant chunks 集合里的**当前版本** child 点（读 payload），按 seq 排序。

    版本切换后旧版本点会留在集合里并标 ``superseded``（延迟清理）。这里必须按
    ``documents.version`` 过滤，否则详情页会把新旧两版混在一起（旧版排前面），
    用户看到的就是"重建了但片段还是旧的"。
    """
    from app.core.vector import qdrant_store

    row = store.get(int(doc_id))
    current = int(dict(row).get("version") or 0) if row is not None else None
    rows = qdrant_store.scroll_filter(
        vector_api.COLLECTION_CHUNKS, {"documents_id": int(doc_id)}, with_vectors=False
    )
    current_rows: list[object] = []
    for r in rows:
        payload = dict(getattr(r, "payload", None) or {})
        if str(payload.get("status") or "") == "superseded":
            continue
        if current is not None and int(payload.get("version") or -1) != current:
            continue
        current_rows.append(r)
    return sorted(current_rows, key=lambda r: int((dict(getattr(r, "payload", None) or {})).get("seq", 0)))


def _count_doc_points(collection: str, doc_id: int) -> int:
    from app.core.vector import qdrant_store

    return len(qdrant_store.scroll_filter(collection, {"documents_id": int(doc_id)}, with_vectors=False))


def _parents_by_key(keys: list[str]) -> dict[str, dict[str, object]]:
    """按 ``parent_key`` **批量**取父块 payload（无向量集合，一次 scroll，不逐条查）。

    父块是"送给模型的证据"（标题 + 该节正文），详情页展开子块时直接展示它，
    用户就不用自己去拼"这个片段属于哪一节、原文长什么样"。
    """
    from app.core.vector import qdrant_store

    unique = [key for key in dict.fromkeys(keys) if key]
    if not unique:
        return {}
    rows = qdrant_store.scroll_filter(
        vector_api.COLLECTION_PARENTS, {"key": unique}, with_vectors=False
    )
    out: dict[str, dict[str, object]] = {}
    for row in rows:
        payload = dict(getattr(row, "payload", None) or {})
        key = str(payload.get("key") or "")
        if key:
            out[key] = payload
    return out


def document_chunks(
    doc_id: int,
    *,
    page: int = 1,
    size: int = 50,
    kind: str | None = None,
    heading: str | None = None,
) -> dict[str, object]:
    """一份资料的**片段列表**（详情页）：分页 + 按类型筛选 + **按标题筛选**。

    数据源改为 Qdrant 的 child payload（SQLite 不再存片段）。
    ``heading`` 是子串匹配（大小写不敏感），匹配完整 ``section_path`` 拼接文本，
    所以输入"相关工作"或"2.1"都能筛到。
    """
    size = max(1, int(size))
    try:
        all_rows = _doc_child_points(int(doc_id))
    except Exception as exc:
        # Qdrant 不可用/集合缺失：详情页片段卡显示空，不崩整页（异常审查）
        logger.warning("读片段失败（doc {}）：{}", doc_id, exc)
        return {"items": [], "total": 0, "page": 1, "pages": 1, "kinds": [], "error": str(exc)}
    payloads = [dict(getattr(r, "payload", None) or {}) for r in all_rows]
    if kind:
        payloads = [p for p in payloads if str(p.get("content_type")) == str(kind)]
    needle = str(heading or "").strip().lower()
    if needle:
        payloads = [
            p
            for p in payloads
            if needle in " › ".join(_section_path_list(p.get("section_path"))).lower()
        ]
    total = len(payloads)
    pages = max(1, -(-total // size))
    current = min(max(1, int(page)), pages)
    start = (current - 1) * size
    window = payloads[start : start + size]
    # 第一个"有章节"的片段 seq：它之前的空章节块 = 文档前置，之后的 = 文档后置
    first_section_seq = min(
        (
            int(payload.get("seq", 0))
            for payload in payloads
            if _section_path_list(payload.get("section_path"))
        ),
        default=None,
    )
    parent_keys = [str(p.get("parent_key")) for p in window if p.get("parent_key")]
    parents = _parents_by_key(parent_keys)
    items = [
        _chunk_item(
            p,
            _section_kind(p, first_section_seq),
            parents.get(str(p.get("parent_key"))),
        )
        for p in window
    ]
    kinds = sorted({str(p.get("content_type") or "text") for p in payloads})
    return {"items": items, "total": total, "page": current, "pages": pages, "kinds": kinds}


def _section_kind(payload: dict[str, object], first_section_seq: int | None) -> str:
    """片段的位置类别：``section`` / ``front_matter`` / ``back_matter``。

    头尾内容（作者、摘要、无标题的尾部）**不伪造章节**，只标位置；``section_path``
    仍然为空，所以章节过滤/结构树不会被污染。
    """
    if _section_path_list(payload.get("section_path")):
        return "section"
    seq = int(payload.get("seq", 0))
    if first_section_seq is None or seq < first_section_seq:
        return "front_matter"
    return "back_matter"


def _chunk_item(
    payload: dict[str, object],
    section_kind: str = "section",
    parent: dict[str, object] | None = None,
) -> dict[str, object]:
    """一行片段的**全部存储信息**（详情页展示"向量里到底存了什么"）。

    除了列表用的摘要，还带：key / 标题链 / 层级 / 父块 key / 字符数 /
    content_hash / policy_version / embedding_version / 原文 / 送进向量的文本。
    """
    from app.core.chunk.index_text import index_text

    text = str(payload.get("text") or "")
    path = _section_path_list(payload.get("section_path"))
    # 每个片段只存**一个**标题字段：完整标题链（面包屑）。没有就为空（头尾内容）
    heading = str(payload.get("heading") or " › ".join(path))
    return {
        "seq": int(payload.get("seq", 0)),
        "page": payload.get("page"),
        "section": path[-1] if path else "",
        "kind": str(payload.get("content_type") or "text"),
        "preview": " ".join(text.split())[:60],
        "key": str(payload.get("key") or ""),
        "section_path": path,
        "section_kind": section_kind,
        "heading": heading,
        "loc": payload.get("loc") or {},
        "level": payload.get("level"),
        "parent_key": str(payload.get("parent_key") or ""),
        # 父块内容（无向量集合里存的"证据"）：详情页展开子块时直接看
        "parent_heading": str(parent.get("heading") or "") if parent else "",
        "parent_text": str(parent.get("text") or "") if parent else "",
        "chars": len(text),
        "content_hash": str(payload.get("content_hash") or ""),
        "policy_version": payload.get("policy_version"),
        "embedding_version": payload.get("embedding_version"),
        "text": text,
        "index_text": index_text(text, path),
    }


def _section_path_list(section_path: object) -> list[str]:
    """``section_path``（JSON 字符串或列表）→ 标题链列表。"""
    if not section_path:
        return []
    try:
        import json

        path = json.loads(section_path) if isinstance(section_path, str) else list(section_path)
        return [str(item) for item in path]
    except Exception:
        return []


def _section_leaf(section_path: object) -> str:
    """section_path 是 JSON 字符串（或列表），取最内层标题。"""
    if not section_path:
        return ""
    try:
        import json

        path = json.loads(section_path) if isinstance(section_path, str) else list(section_path)
        return str(path[-1]) if path else ""
    except Exception:
        return ""
