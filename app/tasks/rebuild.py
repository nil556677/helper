"""重建索引：用副本重新解析与切分（换了切分参数、或解析器升级后用）。

任务标识 = 类名派生 → ``rebuild``。

**幂等**：先删这份资料的片段再写新的，重跑结果一致。

以前重建是在界面回调里**同步**跑的——重新解析一份大 PDF 要好几秒，界面就卡住了；
现在它是一条任务：进度在任务页可见，关窗口也丢不了。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.jobs.model import JobContext, JobError
from app.core.jobs.task import Task, TaskResult
from app.services import knowledge as knowledge_service


class Rebuild(Task):
    """按 ``doc_id`` 重新解析一份资料的副本并重建片段。"""

    title = "重建索引"
    lane = "cpu"          # 同样是解析 + 切分
    resource = "onnx"     # 重建是单遍（解析+切分+重嵌）：必须和导入/重嵌抢同一把 ONNX 串行锁
    idempotent = True     # 先删片段再写新的，重跑结果一样
    max_attempts = 3
    lease_s = 120.0

    class Params(BaseModel):
        """要重建哪一份资料。"""

        doc_id: int

    def run(self, ctx: JobContext, params: Rebuild.Params) -> TaskResult:
        """异常：
            JobError: 副本丢了 / 解析不了——都是内容问题，重试也一样。
        """
        ctx.report(0, 1, "重新解析中…")
        outcome = knowledge_service.rebuild(params.doc_id)
        if not outcome.ok:
            raise JobError(str(outcome.detail or "重建失败"), retryable=False)
        # 重建走单遍 ingest：解析+切分+向量化一次完成，无需再单独排向量任务。
        ctx.report(1, 1, "完成")
        return TaskResult(
            note=f"重建完成（{outcome.chunks} 个片段）",
            detail=f"资料 #{params.doc_id}",
        )

