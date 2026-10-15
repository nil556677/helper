"""知识库的编排（**UI-free**，可以直接被 pytest 调用）。

界面只认这里返回的普通数据；解析、判重、切分全在 core（§5.1 红线 2）。

**导入现在走任务队列**（`run_import_job`）：上传 → `stage_upload` 落 `data/inbox/` →
排一条 `import` 任务 → 任务系统在后台解析入库。这样界面不需要等，关窗口也不丢
（任务表 + inbox 暂存文件，见技术方案 §13）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from app.core import paths
from app.core.db.connection import get_connection
from app.core.features.rag.ingest import parsers, pipeline
from app.core.features.rag.ingest.splitter import CHUNK_MAX_CHARS
from app.core.jobs.model import JobContext, JobError


def supported_formats() -> str:
    """支持哪些格式（**从解析表生成**，避免界面文字与代码漂移）。"""
    return "、".join(parsers.supported_suffixes())


def chunk_limit() -> int:
    """当前片段字数上限（只在界面上说明用，不让用户改；索引期参数等定稿后再谈）。"""
    return CHUNK_MAX_CHARS


def stats() -> dict[str, Any]:
    """概览：就绪份数 / 片段数 / 副本占用 / 最后更新。

    返回：``{"documents": n, "chunks": m, "bytes": b, "updated_at": ts|None, "failed": k}``。
    """
    conn = get_connection()
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM documents WHERE status = 'ready') AS documents,
          (SELECT count(*) FROM documents WHERE status = 'failed') AS failed,
          (SELECT count(*) FROM chunks) AS chunks,
          (SELECT coalesce(sum(size), 0) FROM documents WHERE status = 'ready') AS bytes,
          (SELECT max(updated_at) FROM documents WHERE status = 'ready') AS updated_at
        """
    ).fetchone()
    return {
        "documents": int(row["documents"]),
        "failed": int(row["failed"]),
        "chunks": int(row["chunks"]),
        "bytes": int(row["bytes"]),
        "updated_at": row["updated_at"],
    }


def _document_row(row: Any) -> dict[str, Any]:
    """把一行 documents 变成界面直接能画的字段。"""
    name = str(row["name"])
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    return {
        "id": int(row["id"]),
        "name": name,
        "suffix": suffix.lstrip("."),
        "size": int(row["size"]),
        "chunks": int(row["chunks"]),
        "status": str(row["status"]),
        "detail": str(row["detail"] or ""),
        "updated_at": float(row["updated_at"]),
    }


#: 资料列表的查询（分页与"全都要"共用同一段 SELECT，避免两处漂移）
_DOCUMENTS_SELECT = """
        SELECT d.id AS id, d.source_name AS name, d.size AS size, d.status AS status,
               d.detail AS detail, d.updated_at AS updated_at,
               (SELECT count(*) FROM chunks c WHERE c.doc_id = d.id) AS chunks
        FROM documents d
"""


def documents() -> list[dict[str, Any]]:
    """全部资料（新的在前）。**界面不用它**（列表走 :func:`documents_page` 分页），
    留着是给测试与"导出/统计"这类需要全量的地方用。"""
    rows = get_connection().execute(
        f"{_DOCUMENTS_SELECT} ORDER BY d.updated_at DESC, d.id DESC"
    ).fetchall()
    return [_document_row(row) for row in rows]


def documents_page(*, page: int = 1, size: int = 20, query: str = "") -> dict[str, Any]:
    """一页资料（**服务端分页**：`LIMIT/OFFSET` + 一条 `count(*)`）。

    为什么必须服务端分页：资料可能几千份，"全查出来再切页"又慢又占内存，
    而且总数一多，界面光画 DOM 就得几百毫秒。

    参数：
        page: 页码（1 基；越界会被收回合法范围）。
        size: 每页条数。
        query: 名称筛选（子串匹配，空表示不筛）。
    返回：``{'items', 'total', 'page', 'pages', 'size'}``。
    """
    keyword = query.strip()
    where = "WHERE d.source_name LIKE ?" if keyword else ""
    params: tuple[Any, ...] = (f"%{keyword}%",) if keyword else ()
    conn = get_connection()
    total = int(
        conn.execute(f"SELECT count(*) AS n FROM documents d {where}", params).fetchone()["n"]
    )
    size = max(1, int(size))
    pages = max(1, -(-total // size))
    current = min(max(1, int(page)), pages)
    rows = conn.execute(
        f"{_DOCUMENTS_SELECT} {where} ORDER BY d.updated_at DESC, d.id DESC LIMIT ? OFFSET ?",
        (*params, size, (current - 1) * size),
    ).fetchall()
    return {
        "items": [_document_row(row) for row in rows],
        "total": total,
        "page": current,
        "pages": pages,
        "size": size,
    }


def delete(doc_ids: list[int]) -> int:
    """删掉若干份资料（连带片段与索引）。返回实际删除条数。"""
    return pipeline.delete_documents(doc_ids)


def rebuild(doc_id: int) -> dict[str, Any]:
    """用副本重建某份资料的片段（换了切分参数或解析器升级后用）。"""
    result = pipeline.rebuild_document(doc_id)
    return {"ok": result.status == "imported", "detail": result.reason, "chunks": result.chunks}


# ---------------- 走任务队列的导入（技术方案 §13） ----------------


def stage_upload(name: str, data: bytes) -> dict[str, str]:
    """把上传的字节先落到 ``data/inbox/``，返回导入任务的 payload。

    为什么先落盘：任务表里**只放引用**（路径/id），不放文件字节；而且落了盘之后
    "导到一半关窗口"就变成"下次启动接着导"，而不是白导一场。
    """
    folder = paths.inbox_dir()
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).name).strip(" .") or "unnamed"
    target = folder / f"{uuid4().hex[:12]}_{safe}"
    target.write_bytes(data)
    return {"path": str(target), "name": Path(name).name}


def upload_dedupe_key(data: bytes) -> str:
    """按**内容**算去重键：同一份文件连点两次不会排两条导入任务。"""
    return "import:" + hashlib.sha256(data).hexdigest()[:32]


def run_import_job(ctx: JobContext) -> str:
    """导入任务的 handler（**幂等**：``import_bytes`` 内部三级判重，重跑不会重复入库）。

    参数：
        ctx: 任务上下文（payload 里有 ``path`` 与 ``name``）。
    返回：给用户看的一句话（写进任务的 note）。
    异常：
        JobError: 解析/入库失败——内容问题重试也一样，所以标成不可重试。
    """
    raw_path = str(ctx.payload.get("path") or "")
    name = str(ctx.payload.get("name") or Path(raw_path).name)
    path = Path(raw_path)
    if not raw_path or not path.is_file():
        raise JobError("找不到要导入的文件（可能已被清理），请重新导入", kind="not_found")

    ctx.report(0, 1, "解析中…")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise JobError(f"读不到文件：{exc}", kind="network", retryable=False) from exc

    result = pipeline.import_bytes(data, name)
    _remove_inbox(path)

    if result.status == "failed":
        raise JobError(result.reason or "导入失败", retryable=False)
    ctx.report(1, 1, "完成")
    if result.status == "skipped":
        return "内容已经在资料库里了，跳过"
    note = f"切出 {result.chunks} 个片段"
    if result.notes:
        note = f"{note}（{result.notes[0]}）"
    return note


def _remove_inbox(path: Path) -> None:
    """删掉 inbox 里的暂存文件（成功失败都删：留着只会越积越多）。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("清理暂存文件失败（{}）：{}", path, exc)


def run_rebuild_job(ctx: JobContext) -> str:
    """重建任务的 handler（**幂等**：先删这份资料的片段再写新的，重跑结果一致）。

    以前重建是在界面回调里**同步**跑的——重新解析一份大 PDF 要好几秒，界面就卡住了；
    现在它是一条任务：进度在任务页可见，关窗口也丢不了。
    """
    doc_id = int(ctx.payload.get("doc_id") or 0)
    if not doc_id:
        raise JobError("这条重建任务没有指明是哪份资料", retryable=False)

    ctx.report(0, 1, "重新解析中…")
    outcome = rebuild(doc_id)
    if not outcome["ok"]:
        # 副本丢了 / 解析不了：内容问题，重试也一样
        raise JobError(str(outcome["detail"] or "重建失败"), retryable=False)
    ctx.report(1, 1, "完成")
    return f"重建完成（{outcome['chunks']} 个片段）"
