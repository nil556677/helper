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

from loguru import logger
from pydantic import BaseModel

from app.core.jobs import store
from app.core.jobs.model import JobContext
from app.core.jobs.task import Task, TaskResult
from app.core.logging import purge_old_logs
from app.services import maintenance as maintenance_service

#: 清理分几步（进度按步报，界面上能看出卡在哪一步）
STEPS = 9


class Cleanup(Task):
    """把没人认领的东西收干净（幂等：重跑不会多删或漏删）。"""

    title = "清理存储"
    lane = "cpu"          # 扫目录 + 删文件 + 合并 WAL：IO/CPU 混合的短活
    idempotent = True     # 删的都是"没人认领"的东西，重跑结果一样
    max_attempts = 2
    lease_s = 300.0       # 目录很大时要扫一会儿

    class Params(BaseModel):
        """为什么触发这次清理（只用于留痕与排查）。"""

        reason: str = ""

    def run(self, ctx: JobContext, params: Cleanup.Params) -> TaskResult:
        """逐步报告进度，最后给一句汇总 + 分项详情。"""
        ctx.report(0, STEPS, "找孤儿副本…")
        copies = maintenance_service.remove_orphan_copies()

        ctx.report(1, STEPS, "清 inbox 残留…")
        leftovers = maintenance_service.remove_inbox_leftovers()

        ctx.report(2, STEPS, "合并 WAL…")
        merged = maintenance_service.checkpoint_wal()

        ctx.report(3, STEPS, "清历史记录…")
        purged = store.purge_finished()

        ctx.report(4, STEPS, "清理退场版本…")
        purged_versions = maintenance_service.purge_superseded()

        ctx.report(5, STEPS, "三方对账…")
        report = maintenance_service.reconcile()

        ctx.report(6, STEPS, "状态自愈…")
        # **自愈是尽力而为的一步**：它崩了不该把整条清理任务带崩（真实事故：这里抛
        # AttributeError，整条 cleanup 判失败、其它该清的东西一个没清）。记进详情，继续。
        heal_error = ""
        try:
            healed = maintenance_service.heal_payload_status()
        except Exception as exc:
            healed = {"patched_active": 0, "patched_superseded": 0, "deleted_orphans": 0}
            heal_error = f"状态自愈失败：{exc}"
            logger.exception("状态自愈失败（清理任务继续）")

        ctx.report(7, STEPS, "清孤儿占位行…")
        stale_pending = maintenance_service.remove_stale_pending()

        ctx.report(8, STEPS, "清过期日志…")
        logs = purge_old_logs()
        ctx.report(STEPS, STEPS, "完成")

        note = " · ".join(
            [
                f"孤儿副本 {copies} 个",
                f"暂存 {leftovers} 个",
                f"历史 {purged} 条",
                f"退场版本 {purged_versions['versions']} 版"
                f"（向量 {purged_versions['vectors']} 条）",
                f"孤儿占位 {stale_pending} 行",
                f"过期日志 {logs} 个",
                "WAL 已合并" if merged else "WAL 跳过",
            ]
        )
        counts = report.get("counts") or {}
        detail = (
            f"触发原因：{params.reason or '未说明'}\n"
            f"对账：向量孤儿 {counts.get('vector_orphans', 0)} · "
            f"向量缺失 {counts.get('vector_missing', 0)} · "
            f"无片段资料 {counts.get('documents_without_chunks', 0)} · "
            f"孤儿副本目录 {counts.get('orphan_copies', 0)}\n"
            f"状态自愈：翻 active {healed['patched_active']} · "
            f"翻 superseded {healed['patched_superseded']} · "
            f"删孤儿点 {healed['deleted_orphans']}" + (f"\n{heal_error}" if heal_error else "")
        )
        if report.get("error"):
            # 对账没跑成（Qdrant 暂不可用等）：把原因一起留痕，而不是只留四个 0 让人猜
            detail += f"\n对账未完成：{report['error']}"
        elif any(int(value) for value in counts.values()):  # type: ignore[arg-type]
            detail += f"\n样本：{report}"
        return TaskResult(note=note, detail=detail)
