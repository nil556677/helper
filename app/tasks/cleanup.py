"""清理存储：孤儿副本 / inbox 残留 / WAL / 历史 / 过期日志。

任务标识 = 类名派生 → ``cleanup``。

**为什么做成一条任务而不是"启动时顺手做"**：进度、取消、失败重试、关窗口不丢都是任务系统
白送的；而且它是 **`priority=-10` 的维护类**任务，正好验证这一类（绝不和用户在等的活抢 CPU）。

**三条安全线**（都不是随手定的，见 :mod:`app.services.maintenance`）：

1. **只碰"十分钟没动过"的孤儿副本**：正在导入的资料是"副本已落盘、资料行还没提交"，
   这时它在库里查不到——不等一等就会把正在用的副本删掉；
2. **inbox 里还被任务引用的文件绝不删**：那条任务马上就要读它；
3. **WAL 用 TRUNCATE 合并回主库**：这样"备份 = 复制 data/"才是完整的（§9）。

整个 action **幂等**：删的都是"没人认领"的东西，重跑结果一样，所以重复调用不会累积副作用。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.jobs import store
from app.core.jobs.model import JobContext
from app.core.jobs.task import Task, TaskResult
from app.core.logging import purge_old_logs
from app.services import maintenance as maintenance_service

#: 清理分几步（进度按步报，界面上能看出卡在哪一步）
STEPS = 5


class CleanupTask(Task):
    """把没人认领的东西收干净（幂等：重跑不会多删或漏删）。"""

    title = "清理存储"
    lane = "cpu"          # 扫目录 + 删文件 + 合并 WAL：IO/CPU 混合的短活
    idempotent = True     # 删的都是"没人认领"的东西，重跑结果一样
    max_attempts = 2
    lease_s = 300.0       # 目录很大时要扫一会儿

    class Params(BaseModel):
        """为什么触发这次清理（只用于留痕与排查）。"""

        reason: str = ""

    def run(self, ctx: JobContext, params: CleanupTask.Params) -> TaskResult:
        """逐步报告进度，最后给一句汇总 + 分项详情。"""
        ctx.report(0, STEPS, "找孤儿副本…")
        copies = maintenance_service.remove_orphan_copies()

        ctx.report(1, STEPS, "清 inbox 残留…")
        leftovers = maintenance_service.remove_inbox_leftovers()

        ctx.report(2, STEPS, "合并 WAL…")
        merged = maintenance_service.checkpoint_wal()

        ctx.report(3, STEPS, "清历史记录…")
        purged = store.purge_finished()

        ctx.report(4, STEPS, "清过期日志…")
        logs = purge_old_logs()
        ctx.report(STEPS, STEPS, "完成")

        note = " · ".join(
            [
                f"孤儿副本 {copies} 个",
                f"暂存 {leftovers} 个",
                f"历史 {purged} 条",
                f"过期日志 {logs} 个",
                "WAL 已合并" if merged else "WAL 跳过",
            ]
        )
        return TaskResult(note=note, detail=f"触发原因：{params.reason or '未说明'}")
