"""全量重嵌（``rebuild_vectors``）：换模型/形状后按 Qdrant 里的正文重新嵌入。

遍历 chunks 集合的 child 点，取 payload ``text``，用当前配置的嵌入模型重算 dense 向量，
原样写回（key 不变 → 幂等覆盖）。
"""

from __future__ import annotations

from app.core.jobs.model import JobContext
from app.core.jobs.task import Task, TaskResult
from app.core.vector import api, qdrant_store
from app.core.vector.writer import StoredRecord, store_records


class RebuildVectors(Task):
    title = "全量重嵌"
    lane = "io"
    resource = "onnx"
    idempotent = True
    lease_s = 3600.0

    def run(self, ctx: JobContext, params: object) -> TaskResult:
        del params  # 这个任务没有参数（全量重嵌）
        from app.core.embed import embed_texts

        # **边 scroll 边写**：不要先把所有 active 块的正文+payload 收进内存——
        # 10 万块时那是几百 MB 的峰值（实测 OOM 风险）。这里逐页处理，写完即丢。
        done = 0
        for page in qdrant_store.scroll_all(api.COLLECTION_CHUNKS, with_vectors=False):
            batch: list[tuple[object, str, dict]] = []
            for rec in page:
                payload = dict(rec.payload or {})
                if payload.get("status") != "active":
                    continue
                text = str(payload.get("text") or "")
                if not text:
                    continue
                batch.append((payload.get("key") or rec.id, text, payload))
            if not batch:
                continue
            keys = [k for k, _, _ in batch]
            texts = [t for _, t, _ in batch]
            payloads = [p for _, _, p in batch]
            vectors = embed_texts(texts)
            # 走**统一写入器**：带原 payload 写回（空 payload 会清空元数据），
            # 顺带按当前字段字典刷新动态字段（幂等；attrs_version 落后时自动更新）。
            store_records(
                api.COLLECTION_CHUNKS,
                [
                    StoredRecord(
                        key=str(keys[i]),
                        vector=vectors[i],
                        payload=payloads[i],
                        text=texts[i],
                    )
                    for i in range(len(keys))
                ],
            )
            done += len(batch)
            ctx.report(done, 0, f"已重嵌 {done} 块")  # 总数未知：界面显示不确定进度
        return TaskResult(note=f"已重嵌 {done} 块")
