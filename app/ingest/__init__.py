"""净室 v2 入库：解析/切分算法复用 `core.parse` / `core.chunk`，写入改为 SQLite + Qdrant。

对外入口：`import_path(path, name=...) -> ImportOutcome`。
"""

from __future__ import annotations

from app.ingest.pipeline import ImportOutcome, import_path, import_stored

__all__ = ["ImportOutcome", "import_path", "import_stored"]
