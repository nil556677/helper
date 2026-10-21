"""只重算词法（``rebuild_sparse``）：改词库/分词参数后重建稀疏索引。

词法当前随 Qdrant sparse 落地（P5/后续）。现阶段实现为安全占位：
不删除任何数据，返回"无需重算"。
"""

from __future__ import annotations

from app.core.jobs.model import JobContext
from app.core.jobs.task import Task, TaskResult


class RebuildSparse(Task):
    title = "只重算词法"
    lane = "io"
    idempotent = True
    lease_s = 600.0

    def run(self, ctx: JobContext, params: object) -> TaskResult:
        del params  # 这个任务没有参数
        ctx.report(1, 1, "完成")
        return TaskResult(note="词法 sparse 尚未启用（P5），本次无需重算")
