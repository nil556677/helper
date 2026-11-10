"""记忆清理任务：过期归档 + TTL 治理（幂等，维护类）。

随既有 `cleanup` 同一节奏跑（由 `maintenance.enqueue_memory_cleanup` 在 cleanup 尾部触发），
不新增独立定时器。forward-looking：T4.4 在此追加一致性校验。
"""

from __future__ import annotations  # 延迟值注解

from pydantic import BaseModel  # 参数模型

from app.core.jobs.model import JobContext  # 任务上下文
from app.core.jobs.task import Task, TaskResult  # 任务基类


class MemoryCleanup(Task):
    title = "记忆清理（过期归档）"
    lane = "io"
    idempotent = True
    max_attempts = 2
    lease_s = 120.0

    class Params(BaseModel):
        reason: str = "manual"

    def run(self, ctx: JobContext, params: MemoryCleanup.Params) -> TaskResult:
        del params  # 本任务不需参数细节（reason 只用于展示）
        from app.agent.memory import manager  # 记忆统一入口

        archived = manager.forget_expired()  # 过期归档
        checked = manager.check_consistency()  # 一致性校验（悬浮冲突归档/画像冲突降权/版本裁剪）
        ctx.report(1, 1, "完成")
        detail = f"记忆过期归档 {archived} 条；一致性校验 {sum(checked.values())}（{checked}）"  # 汇总
        return TaskResult(note=detail)
