"""知识库的编排（**UI-free**，可以直接被 pytest 调用）。

界面只认这里返回的普通数据；解析、判重、切分全在 core（§5.1 红线 2）。

**导入现在走任务队列**（任务入口在 `app/tasks/ingest.py` 的 `Ingest`）：上传 →
`stage_upload` 落 `data/inbox/` → 排一条 `ingest` 任务 → 任务系统在后台解析入库。
这样界面不需要等，关窗口也不丢（任务表 + inbox 暂存文件，见技术方案 §13）。

本模块只提供**业务工具**：暂存与判重（`staging`）、查询（`queries`）、删除/重建（`lifecycle`）、
导出（`export`），以及**知识库自己的入队动作**（`queue`：导入 / 全量重建 / 单份重建）。
任务类本身（`Ingest` / `Rebuild` / `Reindex`）在 `app/tasks/` 下，启动时自动发现注册；
`app/tasks/__init__.py` 只管任务系统的装配（发现、注册、执行器生命周期），不掺具体功能。
"""

from ._shared import (
    DocumentPage,
    DocumentView,
    KnowledgeStats,
    RebuildOutcome,
)
from .export import export, export_target
from .lifecycle import delete, rebuild, revectorize, rollback_to_version
from .queries import (
    PREVIEW_CHARS,
    PREVIEW_PDF_PAGES,
    document_chunks,
    document_codes,
    document_detail,
    document_preview,
    documents,
    documents_page,
    stats,
)
from .queue import (
    enqueue_import,
    enqueue_rebuild,
    enqueue_rebuild_many,
    enqueue_reindex,
)
from .staging import (
    MAX_UPLOAD_BYTES,
    UploadTooLarge,
    _register_staged,
    check_size,
    chunk_limit,
    discard_pending,
    inbox_target,
    is_supported_format,
    register_staged,
    stage_upload,
    supported_formats,
    unsupported_hint,
    upload_dedupe_key,
)

__all__ = [
    "MAX_UPLOAD_BYTES",
    "PREVIEW_CHARS",
    "PREVIEW_PDF_PAGES",
    "DocumentPage",
    "DocumentView",
    "KnowledgeStats",
    "RebuildOutcome",
    "UploadTooLarge",
    "_register_staged",
    "check_size",
    "chunk_limit",
    "delete",
    "discard_pending",
    "document_chunks",
    "document_codes",
    "document_detail",
    "document_preview",
    "documents",
    "documents_page",
    "enqueue_import",
    "enqueue_rebuild",
    "enqueue_rebuild_many",
    "enqueue_reindex",
    "export",
    "export_target",
    "inbox_target",
    "is_supported_format",
    "rebuild",
    "register_staged",
    "revectorize",
    "rollback_to_version",
    "stage_upload",
    "stats",
    "supported_formats",
    "unsupported_hint",
    "upload_dedupe_key",
]
