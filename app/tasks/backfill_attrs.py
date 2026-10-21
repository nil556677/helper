"""回填动态字段（``backfill_attrs``）：``attrs_version`` 落后的点按 ``payload.text`` 重算。

**只回填属性，不重嵌入**：改字段字典/提取器/归一化规则后，用这个任务把老数据的
``attrs*`` 补到当前版本（见 docs/动态字段与存储边界改造方案.md §4.5）。

幂等：版本已是最新的点直接跳过；重复跑不会重复写。
"""

from __future__ import annotations

from app.core.attrs.api import build_attrs
from app.core.attrs.version import ATTRS_VERSION
from app.core.jobs.model import JobContext
from app.core.jobs.task import Task, TaskResult
from app.core.vector import api, qdrant_store

#: 空槽也要显式写回：`set_payload` 是合并语义，不写空列表会留下上一版的旧属性。
_SLOTS = ("attrs", "attrs_num", "attrs_flat", "attrs_date")


class BackfillAttrs(Task):
    """把老数据的动态字段回填到当前 ``ATTRS_VERSION``。"""

    title = "回填动态字段"
    lane = "io"
    idempotent = True
    lease_s = 3600.0

    def run(self, ctx: JobContext, params: object) -> TaskResult:
        del params  # 全量回填，无参数
        done = 0
        for page in qdrant_store.scroll_all(api.COLLECTION_CHUNKS, with_vectors=False):
            groups: list[tuple[list[str], dict]] = []
            for record in page:
                payload = dict(record.payload or {})
                if int(payload.get("attrs_version") or 0) >= ATTRS_VERSION:
                    continue
                text = str(payload.get("text") or "")
                if not text:
                    continue
                # 空槽显式清空，避免旧版本的属性残留
                patch: dict = {slot: [] for slot in _SLOTS}
                patch.update(build_attrs(text))
                patch["attrs_version"] = ATTRS_VERSION
                key = payload.get("key") or record.id
                groups.append(([str(key)], patch))
            if groups:
                api.set_payloads(api.COLLECTION_CHUNKS, groups)
                done += len(groups)
                ctx.report(done, 0, f"已回填 {done} 块")
        from app.core.config.store import mark_attrs_version

        mark_attrs_version(force=True)
        return TaskResult(note=f"已回填 {done} 块")
