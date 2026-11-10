"""维护清理（v1）：inbox 残留 + 过期日志（幂等）。"""

from __future__ import annotations

import time

from pydantic import BaseModel

from app.core import paths
from app.core.jobs.model import JobContext
from app.core.jobs.task import Task, TaskResult
from app.core.logging import purge_old_logs

#: inbox 残留 TTL（秒）：一小时没被任务认领就清掉。
INBOX_TTL_S = 3600.0


class Cleanup(Task):
    title = "清理存储"
    lane = "io"
    idempotent = True
    max_attempts = 2
    lease_s = 300.0

    class Params(BaseModel):
        reason: str = "manual"

    def run(self, ctx: JobContext, params: Cleanup.Params) -> TaskResult:
        removed = 0
        cutoff = time.time() - INBOX_TTL_S
        folder = paths.inbox_dir()
        if folder.is_dir():
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError as exc:
                    ctx.add_event("cleanup", f"删除残留失败：{path.name}", str(exc))
        with __import__("contextlib").suppress(Exception):
            purge_old_logs()
        # 顺带触发记忆过期归档（同周期，不新增定时器；去重由 memory_cleanup 的 dedupe_key 保证）
        try:
            from app.services.maintenance import enqueue_memory_cleanup  # 记忆清理入队

            enqueue_memory_cleanup("after_cleanup")  # 排队（幂等去重）
        except Exception:  # 入队失败不影响本任务
            pass  # 忽略
        ctx.report(1, 1, "完成")
        return TaskResult(note=f"清理 inbox 残留 {removed} 个（{params.reason}）")
