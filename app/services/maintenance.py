"""维护动作（v1）：清理任务 + 记忆清理的入队入口。"""

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


def enqueue_memory_cleanup(reason: str = "manual") -> int:
    """排一条记忆清理任务（过期归档；去重，随 cleanup 节奏）。"""
    from app.core.jobs.model import PRIORITY_MAINTENANCE
    from app.tasks.memory_cleanup import MemoryCleanup

    return api.enqueue(
        MemoryCleanup.key(),
        {"reason": reason},
        dedupe_key="memory_cleanup",
        priority=PRIORITY_MAINTENANCE,
    )
