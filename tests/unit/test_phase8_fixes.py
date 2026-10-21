"""阶段 8 修复回归：rebuild_vectors 重嵌必须保留原 payload（不能清空元数据）。"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

SAMPLE = "# 采购合同\n\n第一条 乙方应当交付设备。\n"


def fake_embed(texts: Sequence[str]) -> np.ndarray:
    from app.core.config import store as params_store

    size = int(params_store.load()["embed.dims"])
    rows = []
    for text in texts:
        v = np.zeros(size, dtype=np.float32)
        v[abs(hash(text)) % size] = 1.0
        rows.append(v)
    return np.stack(rows)


def test_rebuild_vectors_preserves_payload(rag_db: None, monkeypatch) -> None:
    from app.core.vector import qdrant_store
    from app.rag.ingest import importer
    from app.tasks.rebuild_vectors import RebuildVectors

    result = importer.import_bytes(SAMPLE.encode(), "合同.md", embed=fake_embed)
    assert result.status == "imported", result.reason

    # 记录写入前的 payload（documents_id/status/text 必须保留）
    before = {}
    for page in qdrant_store.scroll_all("rag_chunks", with_vectors=False):
        for rec in page:
            pl = dict(rec.payload or {})
            before[str(pl.get("key") or rec.id)] = pl
    assert before, "应该有 child 点"

    # 用假嵌入跑 rebuild_vectors（不加载真实模型）
    monkeypatch.setattr("app.core.embed.embed_texts", fake_embed)

    class Ctx:
        def report(self, *a, **k):
            pass

    note = RebuildVectors().run(Ctx(), object()).note

    after = {}
    for page in qdrant_store.scroll_all("rag_chunks", with_vectors=False):
        for rec in page:
            pl = dict(rec.payload or {})
            after[str(pl.get("key") or rec.id)] = pl
    assert note == f"已重嵌 {len(before)} 块"
    for key, pl in before.items():
        assert pl.get("documents_id") == after[key].get("documents_id"), "documents_id 被清空"
        assert pl.get("status") == after[key].get("status"), "status 被清空"
        assert pl.get("text") == after[key].get("text"), "text 被清空"
