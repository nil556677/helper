"""阶段 1 冒烟：单遍 ingest 直接写 Qdrant（注入 fake embed）。"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

SAMPLE = (
    "# 采购合同\n\n第一条 乙方应当在三十日内交付设备并开具发票。\n\n"
    "## 付款\n\n验收合格后付款。\n\n## 违约\n\n逾期按日万分之五计算违约金。\n"
)


def fake_embed(texts: Sequence[str]) -> np.ndarray:
    from app.core.config import store as params_store

    size = int(params_store.load()["embed.dims"])
    rows = []
    for text in texts:
        v = np.zeros(size, dtype=np.float32)
        v[abs(hash(text)) % size] = 1.0
        rows.append(v)
    return np.stack(rows)


def test_ingest_writes_qdrant(rag_db: None) -> None:
    from app.core.vector import api
    from app.rag.ingest import importer

    result = importer.import_bytes(SAMPLE.encode(), "合同.md", embed=fake_embed)
    assert result.status == "imported", result.reason
    assert result.chunks >= 2

    # child 写进 Qdrant，且状态 active
    assert api.count(api.COLLECTION_CHUNKS) > 0
    keys = api.keys(api.COLLECTION_CHUNKS)
    assert keys
    assert all(":" in k for k in keys)


def test_ingest_marks_document_ready(rag_db: None) -> None:
    from app.rag import store
    from app.rag.ingest import importer

    result = importer.import_bytes(SAMPLE.encode(), "合同2.md", embed=fake_embed)
    assert result.doc_id is not None
    row = store.get(int(result.doc_id))
    assert str(row["status"]) == "ready"


def test_ingest_is_idempotent_on_same_bytes(rag_db: None) -> None:
    from app.rag.ingest import importer

    importer.import_bytes(SAMPLE.encode(), "合同3.md", embed=fake_embed)
    second = importer.import_bytes(SAMPLE.encode(), "合同3.md", embed=fake_embed)
    assert second.status == "skipped"
