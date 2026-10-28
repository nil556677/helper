"""维护动作（v1）：目前只保留清理任务的入队入口。"""

from __future__ import annotations

from app.core.jobs import api


def enqueue_cleanup(reason: str = "manual") -> int:
    """排一条清理任务（去重：同一时刻只有一条）。"""
    from app.core.jobs.model import PRIORITY_MAINTENANCE
    from app.tasks.cleanup import Cleanup

    return api.enqueue(
        Cleanup.key(),
        {"reason": reason},
        dedupe_key="cleanup",
        priority=PRIORITY_MAINTENANCE,
    )
