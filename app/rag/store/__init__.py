"""``documents`` 与 ``chunks`` 的**唯一出入库口**（域内所有 SQL 都在这儿）。

为什么必须唯一（两条都由真实故障逼出来）：

1. **chunks 的 ``text`` 与 ``tokens`` 必须同步写**：FTS5 索引的是 ``tokens``、正文在 ``text``，
   两列一旦不同步就是"看得见正文、搜不到内容"——而且**不会报错**，最难查。所以插入只走这里：
   索引文本在函数内部算，调用方**没有机会漏**。
2. **documents 的读写原先散在 pipeline 与多个 service 里**（同一张表的 SQL 出现在三处），
   改一个字段要满仓库找。现在统一收进来，与 ``jobs/store.py``、``config/store.py`` 一个模式。

**事务约定**（照抄既有风格，别混）：

- 形参有 ``conn`` 的 → **不自己开事务**，由调用方决定边界（例如"插资料 + 写片段"要同一个事务）；
- 其余单个逻辑写的 → 自己开事务（``record_failure`` / ``mark_failed`` / ``refresh_file_hash``）。

读函数返回 ``sqlite3.Row``（core 内部允许字符串键；**跨层到 service 时才映射成视图模型**）。

实现按职责拆成子模块（``app/core/features/rag/store/`` 包）：资料行生命周期在
``documents.py``、片段写入在 ``chunks.py``、精确字段在 ``codes.py``、版本链在 ``versions.py``、
统计/列表在 ``stats.py``、共享私有帮助在 ``_shared.py``。对外路径不变
（``from app.rag import store``），以下逐条 re-export 原 ``store`` 模块里的
全部名字，调用方无需改动。
"""

from app.rag.store._shared import _KEEP
from app.rag.store.chunks import (
    all_child_keys,
    all_parent_keys,
    child_keys,
    child_point_payload,
    child_rows,
    delete_chunks,
    live_child_keys,
    parent_keys,
    parent_point_payload,
    promote_chunks,
    rebuild_tokens,
    replace_tree,
    write_tree,
)
from app.rag.store.codes import (
    code_kinds,
    codes_for_document,
    delete_codes,
    ingest_codes,
    lookup_codes,
)
from app.rag.store.documents import (
    all_stored_paths,
    clear_rerank_cache,
    current_row_by_key,
    delete_many,
    doc_key_for,
    find_id_by,
    finish_document,
    get,
    insert_pending,
    insert_ready,
    latest_ready_row,
    mark_failed,
    mark_indexing,
    mark_ready,
    mark_ready_simple,
    next_version,
    paths_in_use,
    record_failure,
    refresh_file_hash,
    set_pipeline_versions,
    set_status,
    superseded_rows,
    versions_of,
    write_summary,
)
from app.rag.store.stats import (
    _LIST_SELECT,
    _SORT_COLUMNS,
    STATUS_FILTERS,
    _name_filter,
    count,
    counts,
    list_all,
    page,
    stored_parent_keys,
    stored_vector_keys,
    vector_bytes,
    vector_coverage,
    vector_progress_by_doc,
)
from app.rag.store.versions import (
    supersede_stale_candidates,
    switch_version,
)

__all__ = [
    "STATUS_FILTERS",
    "_KEEP",
    "_LIST_SELECT",
    "_SORT_COLUMNS",
    "_name_filter",
    "all_child_keys",
    "all_parent_keys",
    "all_stored_paths",
    "child_keys",
    "child_point_payload",
    "child_rows",
    "clear_rerank_cache",
    "code_kinds",
    "codes_for_document",
    "count",
    "counts",
    "current_row_by_key",
    "delete_chunks",
    "delete_codes",
    "delete_many",
    "doc_key_for",
    "find_id_by",
    "finish_document",
    "get",
    "ingest_codes",
    "insert_pending",
    "insert_ready",
    "latest_ready_row",
    "list_all",
    "live_child_keys",
    "lookup_codes",
    "mark_failed",
    "mark_indexing",
    "mark_ready",
    "mark_ready_simple",
    "next_version",
    "page",
    "parent_keys",
    "parent_point_payload",
    "paths_in_use",
    "promote_chunks",
    "rebuild_tokens",
    "record_failure",
    "refresh_file_hash",
    "replace_tree",
    "set_pipeline_versions",
    "set_status",
    "stored_parent_keys",
    "stored_vector_keys",
    "supersede_stale_candidates",
    "superseded_rows",
    "switch_version",
    "vector_bytes",
    "vector_coverage",
    "vector_progress_by_doc",
    "versions_of",
    "write_summary",
    "write_tree",
]
