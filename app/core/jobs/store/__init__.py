"""任务的落库操作：**所有 SQL 都在这里**（引擎其它模块不许直接写 jobs 表）。

三条来自实测与评审的设计事实（技术方案 §13.4、§13.5）：

- **认领索引必须带 ``priority DESC``**：否则 SQLite 会为整个 pending 队列建临时 B 树
  （实测队列 10000 条时认领从 1.02ms 变成 2.14ms）；
- **``runs`` 与 ``fails`` 分开**：崩溃（网络抖动、被强杀）不该耗掉业务重试机会，
  否则"一次抖动 + 两次崩溃"会把一条本来能成功的任务冤枉判死；
- **一次只认领一条**（``LIMIT 1``）：崩溃时最多丢一条的租约，恢复代价最小。

实现按职责拆成子模块（``app/core/jobs/store/`` 包）：任务行 CRUD 在 ``jobs.py``，
认领/续租/释放与崩溃恢复在 ``lease.py``，事件与进度在 ``events.py``，共享私有帮助在
``_shared.py``。对外路径不变（``from app.core.jobs import store``），以下逐条 re-export
原 ``store`` 模块里的全部名字，调用方无需改动。
"""

from app.core.jobs.store._shared import (
    _STATUS_ORDER,
    BACKOFF_BASE_S,
    BACKOFF_MAX_S,
    CRASH_LIMIT,
    DETAIL_MAX_CHARS,
    KEEP_FINISHED_DAYS,
    KEEP_FINISHED_ROWS,
    MESSAGE_MAX_CHARS,
    PAUSED_KEY,
    _clip,
    _insert_event,
)
from app.core.jobs.store.events import add_event, events, record_result, write_progress
from app.core.jobs.store.jobs import (
    active_id_for,
    count_jobs,
    delete_finished,
    delete_jobs,
    enqueue,
    fail,
    finish,
    get_job,
    get_paused,
    is_cancelled,
    list_jobs,
    next_due_at,
    oldest_pending_at,
    pending_of_unknown_kinds,
    purge_finished,
    request_cancel,
    requeue_interrupted,
    requeue_run,
    retry_row,
    set_cursor,
    set_paused,
    settle_cancelled,
    stats,
)
from app.core.jobs.store.lease import (
    _recover,
    claim_next,
    reclaim_expired,
    recover_on_start,
    renew_lease,
)

__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_MAX_S",
    "CRASH_LIMIT",
    "DETAIL_MAX_CHARS",
    "KEEP_FINISHED_DAYS",
    "KEEP_FINISHED_ROWS",
    "MESSAGE_MAX_CHARS",
    "PAUSED_KEY",
    "_STATUS_ORDER",
    "_clip",
    "_insert_event",
    "_recover",
    "active_id_for",
    "add_event",
    "claim_next",
    "count_jobs",
    "delete_finished",
    "delete_jobs",
    "enqueue",
    "events",
    "fail",
    "finish",
    "get_job",
    "get_paused",
    "is_cancelled",
    "list_jobs",
    "next_due_at",
    "oldest_pending_at",
    "pending_of_unknown_kinds",
    "purge_finished",
    "reclaim_expired",
    "record_result",
    "recover_on_start",
    "renew_lease",
    "request_cancel",
    "requeue_interrupted",
    "requeue_run",
    "retry_row",
    "set_cursor",
    "set_paused",
    "settle_cancelled",
    "stats",
    "write_progress",
]
