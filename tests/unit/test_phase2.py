"""阶段 2 冒烟：schema v12（SQLite 无 chunks/codes）+ 详情/片段/统计走 Qdrant。"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

SAMPLE = "# 采购合同\n\n第一条 乙方应当在三十日内交付设备。\n\n## 付款\n\n验收合格后付款。\n"


def fake_embed(texts: Sequence[str]) -> np.ndarray:
    from app.core.config import store as params_store

    size = int(params_store.load()["embed.dims"])
    rows = []
    for text in texts:
        v = np.zeros(size, dtype=np.float32)
        v[abs(hash(text)) % size] = 1.0
        rows.append(v)
    return np.stack(rows)


def test_schema_v12_has_no_chunk_tables(ingest_db: None) -> None:
    from app.core.db.connection import get_connection
    from app.core.db.schema import SCHEMA_VERSION, schema_version, table_names

    assert schema_version(get_connection()) == SCHEMA_VERSION
    names = table_names(get_connection())
    for gone in ("chunks", "chunks_fts", "codes", "codes_fts", "matrices", "vector_keys"):
        assert gone not in names, f"{gone} 不应存在"
    assert "documents" in names


def test_document_detail_and_chunks_from_qdrant(rag_db: None) -> None:
    from app.rag.ingest import importer
    from app.services import knowledge as knowledge_service

    result = importer.import_bytes(SAMPLE.encode(), "合同.md", embed=fake_embed)
    assert result.status == "imported", result.reason
    doc_id = int(result.doc_id)

    detail = knowledge_service.document_detail(doc_id)
    assert detail is not None
    assert detail["children"] > 0

    chunks = knowledge_service.document_chunks(doc_id, page=1, size=50)
    assert chunks["total"] > 0
    assert chunks["items"]
    # 子块要能带出父块（证据正文）：详情页展开时直接看父块
    assert any(item["parent_text"] for item in chunks["items"])  # type: ignore[index]


def test_counts_work_without_chunk_table(rag_db: None) -> None:
    from app.rag import store
    from app.rag.ingest import importer

    importer.import_bytes(SAMPLE.encode(), "合同2.md", embed=fake_embed)
    counts = store.counts()
    assert counts["documents"] >= 1
    assert "chunks" in counts
