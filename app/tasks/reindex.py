"""全量重建索引（P1.8 的"重活"那条路）：按当前参数重新解析、切分、建向量。

三种重建的边界（别混）：

| 场景 | 入口 | 代价 |
| --- | --- | --- |
| 只换了词库/分词参数 | ``rebuilder.rebuild_lexical``（**只重算词法**） | 秒级，不动向量 |
| 换了切分参数 / 解析器升级 | ``rebuilder.rebuild_document``（一份资料） | 重新解析 + 重新切分 + **要重建向量** |
| 换了嵌入模型 / 维度 / 整体重建 | 本任务（全量） | 小时级（模型推理是瓶颈） |

**为什么不给每份资料各排一条 embed 任务**：全量重建要的是"一个整体进度 + 一个整体失败率"，
拆成 N 条小任务后用户看到的是一屏碎片，而且中途失败也说不清"整体到哪了"。
所以这里**串行**做：一份资料解析完就立刻向量化，然后才下一份。
"""

from __future__ import annotations

from loguru import logger
from pydantic import BaseModel

from app.core.jobs.model import JobCancelled, JobContext, JobInterrupted
from app.core.jobs.task import Task, TaskResult
from app.rag import store
from app.rag.ingest import rebuilder


class Reindex(Task):
    """全量重建（重新解析 + 重新切分 + 重建向量）。"""

    title = "全量重建索引"

    lane = "cpu"

    #: 重建会重新向量化（单遍 ingest 内联了嵌入）：与导入/重嵌共用同一把 ONNX 串行锁，
    #: 否则两条任务会各开一份 intra-op 线程（6 → 12），正好踩"超过 6 线程反而更慢"的坑。
    resource = "onnx"

    #: 幂等：每份资料都是"删片段重写 + 覆盖同一批向量点"。
    idempotent = True

    #: **不自动重试**：全量重建可能跑一小时，失败后自动重来一遍只会让用户更迷惑。
    max_attempts = 1

    #: 一份大资料就要几分钟：租约给足，别被当成卡死回收（那会同时跑两份重建）。
    lease_s = 300.0

    class Params(BaseModel):
        """要重建哪些资料（缺省 = 库里全部）。"""

        doc_ids: list[int] | None = None

    def run(self, ctx: JobContext, params: Reindex.Params) -> TaskResult:
        """逐份重建（一份做完再做下一份，失败不中断整体）。"""
        rows = store.list_all()
        if params.doc_ids is not None:
            wanted = set(params.doc_ids)
            rows = [row for row in rows if int(row["id"]) in wanted]
        if not rows:
            return TaskResult(note="没有需要重建的资料")

        total = len(rows)
        failures: list[str] = []
        done = 0
        for index, row in enumerate(rows, start=1):
            doc_id = int(row["id"])
            name = str(row["name"])  # list_all 的别名（不是 source_name）
            ctx.report(index - 1, total, f"重建 {name}")
            try:
                result = rebuilder.rebuild_document(doc_id)
            except (JobCancelled, JobInterrupted):
                raise  # 取消/退出：中断整批，别当普通失败累计继续空转（异常审查）
            except Exception as exc:  # 单份炸了不能拖垮整批（全量重建可能跑一小时）
                logger.warning("重建失败（{}）：{}", name, exc)
                failures.append(f"{name}：{exc}")
                continue
            if result.status != "imported":
                failures.append(f"{name}：{result.reason or '重建失败'}")
                continue
            done += 1  # 单遍 rebuild_document 已内联向量化，无需再单独排向量任务

        ctx.report(total, total, "完成")
        note = f"重建 {done}/{total} 份"
        if failures:
            note += f"，失败 {len(failures)} 份"
        detail = "\n".join(failures[:20]) if failures else "全部成功"
        return TaskResult(note=note, detail=detail)
