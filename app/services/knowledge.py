"""知识库的编排（**UI-free**，可以直接被 pytest 调用）。

界面只认这里返回的普通数据；解析、判重、切分全在 core（§5.1 红线 2）。

**导入现在走任务队列**（任务入口在 `app/tasks/import_doc.py` 的 `ImportTask`）：上传 →
`stage_upload` 落 `data/inbox/` → 排一条 `import` 任务 → 任务系统在后台解析入库。
这样界面不需要等，关窗口也不丢（任务表 + inbox 暂存文件，见技术方案 §13）。

本模块只提供**业务工具**（暂存、判重键、查询、删除、重建）；任务的声明与执行入口
都在 `app/tasks/` 下（一个任务一个类，启动时自动发现注册）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core import paths
from app.core.features.rag.ingest import pipeline, store
from app.core.features.rag.ingest.splitter import CHUNK_MAX_CHARS
from app.core.parse import supported_suffixes

# ---------------------------------------------------------------------------
# 视图模型（界面只读它们；`from_row` 是「行 → 视图」的唯一映射处）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentView:
    """资料列表里的一行。"""

    id: int
    name: str
    suffix: str
    size: int
    chunks: int
    status: str
    detail: str
    updated_at: float

    @classmethod
    def from_row(cls, row: Any) -> DocumentView:
        """一行 documents → 界面直接能画的字段（**唯一**映射处）。"""
        name = str(row["name"])
        suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        return cls(
            id=int(row["id"]),
            name=name,
            suffix=suffix.lstrip("."),
            size=int(row["size"]),
            chunks=int(row["chunks"]),
            status=str(row["status"]),
            detail=str(row["detail"] or ""),
            updated_at=float(row["updated_at"]),
        )


@dataclass(frozen=True)
class KnowledgeStats:
    """概览：就绪份数 / 失败份数 / 片段数 / 副本占用 / 最后更新。"""

    documents: int
    failed: int
    chunks: int
    bytes: int
    updated_at: float | None


@dataclass(frozen=True)
class DocumentPage:
    """资料列表的一页（服务端分页的结果）。"""

    items: tuple[DocumentView, ...]
    total: int
    page: int
    pages: int
    size: int


@dataclass(frozen=True)
class RebuildOutcome:
    """重建一份资料的结果。"""

    ok: bool
    detail: str
    chunks: int


def supported_formats() -> str:
    """支持哪些格式（**从解析表生成**，避免界面文字与代码漂移）。"""
    return "、".join(supported_suffixes())


def chunk_limit() -> int:
    """当前片段字数上限（只在界面上说明用，不让用户改；索引期参数等定稿后再谈）。"""
    return CHUNK_MAX_CHARS


def stats() -> KnowledgeStats:
    """概览：就绪份数 / 片段数 / 副本占用 / 最后更新（SQL 在 ingest store 里）。"""
    row = store.counts()
    return KnowledgeStats(
        documents=int(row["documents"]),
        failed=int(row["failed"]),
        chunks=int(row["chunks"]),
        bytes=int(row["bytes"]),
        updated_at=row["updated_at"],
    )


def documents() -> tuple[DocumentView, ...]:
    """全部资料（新的在前）。**界面不用它**（列表走 :func:`documents_page` 分页），
    留着是给测试与"导出/统计"这类需要全量的地方用。"""
    return tuple(DocumentView.from_row(row) for row in store.list_all())


def documents_page(*, page: int = 1, size: int = 20, query: str = "") -> DocumentPage:
    """一页资料（**服务端分页**：`LIMIT/OFFSET` + 一条 `count(*)`）。

    为什么必须服务端分页：资料可能几千份，"全查出来再切页"又慢又占内存，
    而且总数一多，界面光画 DOM 就得几百毫秒。

    参数：
        page: 页码（1 基；越界会被收回合法范围）。
        size: 每页条数。
        query: 名称筛选（子串匹配，空表示不筛）。
    """
    total = store.count(query)
    size = max(1, int(size))
    pages = max(1, -(-total // size))
    current = min(max(1, int(page)), pages)
    rows = store.page(query, size=size, offset=(current - 1) * size)
    return DocumentPage(
        items=tuple(DocumentView.from_row(row) for row in rows),
        total=total,
        page=current,
        pages=pages,
        size=size,
    )


def delete(doc_ids: list[int]) -> int:
    """删掉若干份资料（连带片段与索引）。返回实际删除条数。"""
    return pipeline.delete_documents(doc_ids)


def rebuild(doc_id: int) -> RebuildOutcome:
    """用副本重建某份资料的片段（换了切分参数或解析器升级后用）。"""
    result = pipeline.rebuild_document(doc_id)
    return RebuildOutcome(
        ok=result.status == "imported",
        detail=result.reason,
        chunks=result.chunks,
    )


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


#: 任务 handler 不在这里：导入/重建的入口搬到了 ``app/tasks/import_doc.py`` 与
#: ``app/tasks/rebuild.py``（每个任务一个模块，自带 ``KIND`` 声明，启动时自动发现）。
#: 本模块只留**业务工具**：暂存、判重键、查询、删除、重建。

