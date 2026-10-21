"""重构后核心行为测试：版本切换、删除、崩溃重入、统计、任务 kinds、point id 守卫。

这些是重构后最容易被破坏的路径（Qdrant payload 状态机 + SQLite documents 状态），
没有测试钉住，一次误改就会静默破坏检索/清理。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pytest

SAMPLE = "# 采购合同\n\n第一条 乙方应当在三十日内交付设备并开具发票。\n\n## 付款\n\n验收合格后付款。\n"


def fake_embed(texts: Sequence[str]) -> np.ndarray:
    from app.core.config import store as params_store

    size = int(params_store.load()["embed.dims"])
    rows = []
    for text in texts:
        v = np.zeros(size, dtype=np.float32)
        v[abs(hash(text)) % size] = 1.0
        rows.append(v)
    return np.stack(rows)


def _points_by_key(collection: str) -> dict[str, dict]:
    from app.core.vector import qdrant_store

    out: dict[str, dict] = {}
    for page in qdrant_store.scroll_all(collection, with_vectors=False):
        for rec in page:
            pl = dict(rec.payload or {})
            out[str(pl.get("key") or rec.id)] = pl
    return out


def _import(body: str, name: str) -> int:
    from app.rag.ingest import importer

    result = importer.import_bytes(body.encode(), name, embed=fake_embed)
    assert result.status == "imported", result.reason
    return int(result.doc_id)  # type: ignore[arg-type]


# ---------------- 版本切换（先向量后 DB 的状态一致性） ----------------


def test_version_switch_flips_qdrant_and_sqlite(rag_db: None) -> None:
    from app.core.vector import api
    from app.rag import store

    first = _import(SAMPLE, "合同.md")
    second = _import(SAMPLE.replace("三十日", "四十五日"), "合同.md")

    assert str(store.get(first)["status"]) == "superseded"
    assert str(store.get(second)["status"]) == "ready"

    # Qdrant：旧版本点 superseded、新版本点 active
    chunks = _points_by_key(api.COLLECTION_CHUNKS)
    old_active = [k for k, p in chunks.items() if k.startswith(f"{first}:") and p.get("status") == "active"]
    new_active = [k for k, p in chunks.items() if k.startswith(f"{second}:") and p.get("status") == "active"]
    assert not old_active, "旧版本的点不应再 active"
    assert new_active, "新版本的点应 active"


# ---------------- 删除（Qdrant chunks + parents 一起清） ----------------


def test_delete_removes_points_from_both_collections(rag_db: None) -> None:
    from app.core.vector import api
    from app.rag.ingest import importer

    doc_id = _import(SAMPLE, "合同.md")
    importer.delete_documents([doc_id])

    for collection in (api.COLLECTION_CHUNKS, api.COLLECTION_PARENTS):
        keys = _points_by_key(collection)
        assert not any(k.startswith(f"{doc_id}:") for k in keys), f"{collection} 残留 {doc_id} 的点"


# ---------------- 崩溃重入（indexing 允许重跑） ----------------


def test_ingest_reentry_allowed_when_indexing(rag_db: None, tmp_path) -> None:
    """行卡在 indexing（上次崩溃半写）时，重跑不应判"重复投递"，而应重写并转 ready。"""
    from app.core.db.connection import transaction
    from app.core.vector import api
    from app.rag import store
    from app.rag.ingest import importer

    # 手工造一个 indexing 状态的占位行（模拟崩溃残留）
    with transaction() as conn:
        doc_id = store.insert_ready(
            conn,
            source_name="合同.md",
            stored_path="abc/合同.md",
            file_hash="crashhash",
            text_hash=None,
            size=3,
            detail=None,
            status="indexing",
            now=1.0,
        )
    staged = tmp_path / "合同.md"
    staged.write_bytes(SAMPLE.encode())
    result = importer.import_path(staged, "合同.md", embed=fake_embed, claim_doc_id=doc_id)
    assert result.status == "imported", result.reason
    # 重入后 Qdrant 有该资料的点，文档就绪
    chunks = _points_by_key(api.COLLECTION_CHUNKS)
    assert any(k.startswith(f"{doc_id}:") for k in chunks)
    assert str(store.get(doc_id)["status"]) == "ready"


# ---------------- 统计 ----------------


def test_counts_and_detail_after_ingest(rag_db: None) -> None:
    from app.rag import store
    from app.services import knowledge as knowledge_service

    doc_id = _import(SAMPLE, "合同.md")
    counts = store.counts()
    assert counts["documents"] >= 1
    assert counts["chunks"] > 0

    detail = knowledge_service.document_detail(doc_id)
    assert detail is not None
    assert detail["children"] > 0
    assert detail["parents"] >= 1
    assert detail["ocr_pages"] == ()  # 文本文件无 OCR 页，且安全解析不崩


# ---------------- 任务 kinds ----------------


def test_kinds_register(rag_db: None) -> None:
    from app import tasks as tasks_app
    from app.core.jobs import api as jobs_api
    from app.core.jobs import registry

    registry.clear()
    tasks_app.register_kinds()
    keys = {spec.key for spec in jobs_api.kinds()}
    assert {"cleanup", "ingest", "rebuild", "rebuild_sparse", "rebuild_vectors", "reindex"} <= keys
    assert "embed" not in keys, "embed 任务已合并进 ingest"


# ---------------- point id 守卫 ----------------


def test_point_id_guard_rejects_overflow() -> None:
    from app.core.vector.qdrant_store import point_id

    point_id(1, 1, 5)  # 正常
    with pytest.raises(ValueError):
        point_id(1, 1, 100_000)  # seq 溢出
    with pytest.raises(ValueError):
        point_id(1, 10_000_000, 1)  # version 溢出


def test_chunk_count_matches_qdrant_and_seq_is_dense(rag_db: None) -> None:
    """切出的子块数必须等于 Qdrant 里的 child 点数，且 seq 连续（0..n-1）。

    UI 真机核验发现过：父子块共用 seq 导致子块编号跳号、汇报的"片段数"虚高。
    """
    from app.core.vector import api, qdrant_store

    doc_id = _import(SAMPLE, "合同.md")
    child_keys = sorted(
        str(dict(rec.payload or {}).get("key"))
        for page in qdrant_store.scroll_all(api.COLLECTION_CHUNKS, with_vectors=False)
        for rec in page
        if int(dict(rec.payload or {}).get("documents_id", -1)) == doc_id
    )
    seqs = sorted(int(key.split(":")[2]) for key in child_keys)
    assert seqs == list(range(len(seqs))), f"子块 seq 应连续：{seqs}"
    assert api.count(api.COLLECTION_CHUNKS) == len(child_keys)
