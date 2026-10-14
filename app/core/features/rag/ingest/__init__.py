"""资料导入：文件 → 可检索片段。

对外只暴露 :func:`app.core.features.rag.ingest.pipeline` 里的四件事：

- ``import_bytes`` / ``import_file``：导一份资料；
- ``rebuild_document``：用副本重新解析与切分；
- ``delete_documents``：删资料（连带片段与索引，由外键与触发器负责）。
"""

from app.core.features.rag.ingest.pipeline import (
    ImportResult,
    delete_documents,
    import_bytes,
    import_file,
    rebuild_document,
)

__all__ = [
    "ImportResult",
    "delete_documents",
    "import_bytes",
    "import_file",
    "rebuild_document",
]
