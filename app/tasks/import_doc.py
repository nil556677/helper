"""导入资料：解析 + 切分 + 词法入库（走队列，见技术方案 §13）。

任务标识 = 类名派生 → ``import``。

**幂等**：``pipeline.import_bytes`` 内部三级判重，重跑不会重复入库。

**资源清理（这条是重点）**：上传的暂存文件落在 ``data/inbox/``，任务无论**成功、失败、
还是抛异常**都要把它删掉——否则用户每失败重试一次就多留一份残留，inbox 会越积越大。
所以删除放在 ``finally`` 里，而不是"成功路径顺手删一下"。

删除还**只认 inbox 目录内的文件**：参数里的 path 理论上可以指向任何地方，
无条件 ``unlink`` 会误删用户自己的文件。宁可留个告警，也不能删错。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic import BaseModel

from app.core import paths
from app.core.features.rag.ingest import pipeline
from app.core.jobs.model import JobContext, JobError
from app.core.jobs.task import Task, TaskResult


def discard_staged(path: Path) -> None:
    """删掉暂存文件（**仅限 inbox 内**；成功失败都该删）。

    为什么限定目录：参数里的 ``path`` 由任务创建者给，不能假定它一定是我们的暂存文件。
    不在 inbox 里就只告警、不删——这类"清理动作误伤用户数据"的代价远大于漏删一个临时文件。
    """
    try:
        inbox = paths.inbox_dir().resolve()
        target = path.resolve()
    except OSError as exc:
        logger.warning("暂存文件路径解析失败（{}）：{}", path, exc)
        return
    if target.parent != inbox:
        logger.warning("暂存文件不在 inbox 里，未删除：{}", path)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("清理暂存文件失败（{}）：{}", path, exc)


class ImportTask(Task):
    """把 inbox 里的一份文件解析入库。"""

    title = "导入资料"
    lane = "cpu"          # 解析/切分是 CPU 活（第三方解析库都是同步的）
    idempotent = True     # 内部三级判重，重跑安全
    max_attempts = 3
    lease_s = 120.0       # 大 PDF 解析可能几十秒

    class Params(BaseModel):
        """要导入的暂存文件（只放引用：路径与原始文件名）。"""

        path: str
        name: str = ""

    def run(self, ctx: JobContext, params: ImportTask.Params) -> TaskResult:
        """异常：
            JobError: 解析/入库失败——内容问题重试也一样，所以标成不可重试。
        """
        path = Path(params.path)
        name = params.name or path.name
        if not params.path or not path.is_file():
            # 文件本来就不在：没有暂存物要清，直接判失败
            raise JobError("找不到要导入的文件（可能已被清理），请重新导入", kind="not_found")

        ctx.report(0, 1, "解析中…")
        try:
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise JobError(f"读不到文件：{exc}", kind="network", retryable=False) from exc

            result = pipeline.import_bytes(data, name)

            if result.status == "failed":
                raise JobError(result.reason or "导入失败", retryable=False)
            ctx.report(1, 1, "完成")
            if result.status == "skipped":
                return TaskResult(note="内容已经在资料库里了，跳过")
            extra = f"；{result.notes[0]}" if result.notes else ""
            return TaskResult(
                note=f"切出 {result.chunks} 个片段",
                detail=f"文件：{name}{extra}",
            )
        finally:
            # 成功 / 失败 / 异常都清：重复调用（重试、重新导入）不会留下残留
            discard_staged(path)
