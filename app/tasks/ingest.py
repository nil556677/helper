"""**单遍 ingest**：解析 + 切分 + 规则提取 + 向量化 + 写 Qdrant（走队列）。

任务标识 = 类名派生 → ``ingest``。原 ``import_doc`` + ``embed`` 合并为此一个 kind。

**幂等**：``importer.import_bytes`` 内部多级判重，重跑不会重复入库。

**资源清理**：上传的暂存文件落在 ``data/inbox/``，任务无论成功/失败/异常都要删掉，
删除只认 inbox 目录内的文件，避免误删用户数据。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic import BaseModel

from app.core import paths
from app.core.jobs.model import JobCancelled, JobContext, JobError, JobInterrupted
from app.core.jobs.task import Task, TaskResult
from app.ingest import import_path


def discard_staged(path: Path) -> None:
    """删掉暂存文件（**仅限 inbox 内**；成功失败都该删）。"""
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


def _failure(result: object) -> JobError:
    """把导入失败变成任务失败。

    **重试策略按 `error_kind` 判定**（P7 审查修正，之前有两条是反的）：
    - 可重试 = **真·瞬时故障**：``io``（写副本/磁盘抖动）、``timeout``；
    - 不可重试 = 内容/配置问题（``unsupported`` / ``bad_request`` / ``not_found`` /
      ``encoding`` / ``model`` / ``unknown``）——重试三次只是浪费用户时间。
      尤其是 ``unknown``（未预料异常，多为必现 bug）与 ``encoding``（坏字节）、
      以及 ``model``（模型缺失/SHA 不符/onnxruntime 起不来），重试都不会变好。
    - ``_parse_kind`` 已给出结构化分类；这里只在**完全没有 kind** 时兜底成 bad_request。
    """
    reason = str(getattr(result, "reason", "") or "导入失败")
    kind = str(getattr(result, "error_kind", "") or "bad_request")
    retryable = kind in ("io", "timeout")
    return JobError(reason, kind=kind, retryable=retryable)


class Ingest(Task):
    """把 inbox 里的一份文件解析、切分、向量化入库（一次完成）。"""

    title = "导入资料"
    lane = "io"            # 含文件解析(IO) + 向量化；向量化用 resource 串行
    resource = "onnx"      # 独占嵌入模型会话
    idempotent = True
    max_attempts = 3
    lease_s = 600.0        # 解析 + 向量化可能几分钟

    class Params(BaseModel):
        path: str
        name: str = ""
        doc_id: int | None = None

    def run(self, ctx: JobContext, params: Ingest.Params) -> TaskResult:
        path = Path(params.path)
        name = params.name or path.name
        if not params.path or not path.is_file():
            raise JobError("找不到要导入的文件（可能已被清理），请重新导入", kind="not_found")

        ctx.report(0, 1, "解析并向量化…")
        # **暂存文件不是一律删**：失败要分两种——
        # - 内容问题（不可重试）：留着没用，删掉；
        # - 基础设施抖动 / 应用退出（可重试、下次接着做）：**必须留着**，否则重试时
        #   `path.is_file()` 直接 not_found，任务必死（实测：关一次窗口就让导入永久失败）。
        #   真正的残留由 maintenance 的 remove_inbox_leftovers（1 小时 TTL）收尾。
        keep_staged = False
        try:
            result = import_path(
                path,
                name,
                on_progress=lambda done, total, note: ctx.report(done, total, note or "处理中…"),
                claim_doc_id=params.doc_id,
            )
            if result.status == "failed":
                raise _failure(result)
            if result.status == "skipped":
                ctx.report(1, 1, "跳过")
                return TaskResult(note="内容已经在资料库里了，跳过")

            # 切分诊断：**错误不静默**——每条都写进任务事件，界面可见
            from app.core.chunk.diagnostics import Severity

            diagnostics = tuple(getattr(result, "diagnostics", ()) or ())
            for diagnostic in diagnostics:
                message = diagnostic.message
                if diagnostic.count > 1:
                    message = f"{message}（{diagnostic.count} 处）"
                ctx.add_event(diagnostic.code, message, diagnostic.detail)
            errors = [item for item in diagnostics if item.severity is Severity.ERROR]
            if errors:
                raise JobError(
                    f"切分发现 {len(errors)} 类错误：{errors[0].message}",
                    kind="chunking",
                    retryable=False,
                )

            ctx.report(1, 1, "完成")
            extra = f"；{result.doc_title}" if result.doc_title else ""
            note = f"切出 {result.chunks} 个片段"
            if diagnostics:
                note += f"（含 {len(diagnostics)} 条告警）"
            return TaskResult(note=note, detail=f"文件：{name}{extra}")
        except (JobCancelled, JobInterrupted):
            keep_staged = True  # 中断会回队列重跑：文件得留着
            raise
        except JobError as exc:
            keep_staged = bool(exc.retryable)
            raise
        except Exception:
            keep_staged = True  # 未预料异常由引擎按可重试处理：别把重试要用的文件删了
            raise
        finally:
            if not keep_staged:
                discard_staged(path)
