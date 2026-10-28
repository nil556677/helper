"""全量重建索引（v1）：逐份走新 ingest pipeline（解析/切分/向量化）。"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.jobs.model import JobCancelled, JobContext, JobInterrupted
from app.core.jobs.task import Task, TaskResult


class Reindex(Task):
    title = "全量重建索引"
    lane = "cpu"
    resource = "onnx"
    idempotent = True
    max_attempts = 1
    lease_s = 300.0

    class Params(BaseModel):
        doc_ids: list[int] | None = None

    def run(self, ctx: JobContext, params: Reindex.Params) -> TaskResult:
        from app.core.db.connection import get_connection
        from app.services import knowledge as knowledge_service

        rows = get_connection().execute(
            "SELECT id, name FROM documents ORDER BY id"
        ).fetchall()
        if params.doc_ids is not None:
            wanted = {int(item) for item in params.doc_ids}
            rows = [row for row in rows if int(row["id"]) in wanted]
        if not rows:
            return TaskResult(note="没有需要重建的资料")

        total = len(rows)
        failures: list[str] = []
        done = 0
        for index, row in enumerate(rows, start=1):
            doc_id = int(row["id"])
            name = str(row["name"])
            ctx.report(index - 1, total, f"重建 {name}")
            try:
                outcome = knowledge_service.rebuild(doc_id)
            except (JobCancelled, JobInterrupted):
                raise
            except Exception as exc:
                failures.append(f"{name}：{exc}")
                continue
            if not outcome.ok:
                failures.append(f"{name}：{outcome.detail or '重建失败'}")
                continue
            done += 1
        ctx.report(total, total, "完成")
        note = f"重建 {done}/{total} 份"
        if failures:
            note += f"，失败 {len(failures)} 份"
        return TaskResult(note=note, detail="\n".join(failures[:20]) if failures else "全部成功")
