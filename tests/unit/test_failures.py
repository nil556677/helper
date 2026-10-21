"""失败路径与"删除/并发"边界：embed 失败、双导、pending 中删除。"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

SAMPLE = "# 采购合同\n\n第一条 乙方应当在三十日内交付设备并开具发票。\n\n## 付款\n\n验收合格后付款。\n"


def good_embed(texts: Sequence[str]) -> np.ndarray:
    from app.core.config import store as params_store

    size = int(params_store.load()["embed.dims"])
    rows = []
    for text in texts:
        v = np.zeros(size, dtype=np.float32)
        v[abs(hash(text)) % size] = 1.0
        rows.append(v)
    return np.stack(rows)


def raising_embed(texts: Sequence[str]) -> np.ndarray:
    raise RuntimeError("模型不可用")


def _chunk_keys(collection: str) -> set[str]:
    from app.core.vector import qdrant_store

    out: set[str] = set()
    for page in qdrant_store.scroll_all(collection, with_vectors=False):
        for rec in page:
            pl = dict(rec.payload or {})
            out.add(str(pl.get("key") or rec.id))
    return out


def test_ingest_embed_failure_marks_failed_no_active_points(rag_db: None) -> None:
    from app.core.vector import api
    from app.rag import store
    from app.rag.ingest import importer

    result = importer.import_bytes(SAMPLE.encode(), "合同.md", embed=raising_embed)
    assert result.status == "failed"
    doc_id = int(result.doc_id)  # type: ignore[arg-type]
    assert str(store.get(doc_id)["status"]) == "failed"
    # 没有 active 点（不产生"半截可搜"）
    chunks = _chunk_keys(api.COLLECTION_CHUNKS)
    assert not chunks, "embed 失败不应留下可搜的点"

    # 用正常 embed 重导入（同一份）：重新生成并就绪
    second = importer.import_bytes(SAMPLE.encode(), "合同.md", embed=good_embed)
    assert second.status == "imported"
    assert str(store.get(int(second.doc_id))["status"]) == "ready"  # type: ignore[arg-type]


def test_double_import_same_bytes_is_idempotent(rag_db: None) -> None:
    from app.core.vector import api
    from app.rag.ingest import importer

    first = importer.import_bytes(SAMPLE.encode(), "合同.md", embed=good_embed)
    assert first.status == "imported"
    count_before = api.count(api.COLLECTION_CHUNKS)
    second = importer.import_bytes(SAMPLE.encode(), "合同.md", embed=good_embed)
    assert second.status == "skipped", "同字节重复导入应跳过"
    assert api.count(api.COLLECTION_CHUNKS) == count_before, "重复导入不产生新点"


def test_delete_while_pending_leaves_no_orphans(rag_db: None) -> None:
    """删除一份"只建到一半"（pending 点 + indexing 行）的资料：行与点一起清。"""
    from app.core.db.connection import transaction
    from app.core.vector import api
    from app.rag import store
    from app.rag.ingest import importer

    # 手工造 indexing 行 + 写入 pending 点（模拟崩溃残留的中间态）
    with transaction() as conn:
        doc_id = store.insert_ready(
            conn,
            source_name="合同.md",
            stored_path="abc/合同.md",
            file_hash="x",
            text_hash=None,
            size=3,
            detail=None,
            status="indexing",
            now=1.0,
        )
    from app.core.vector import qdrant_store

    qdrant_store.upsert_points_vectorless(
        "rag_parents",
        [
            ("1:1:0", {"documents_id": doc_id, "version": 1, "status": "pending", "text": "父块"}),
        ],
    )
    importer.delete_documents([doc_id])

    assert store.get(doc_id) is None, "行要删掉"
    assert api.count(api.COLLECTION_PARENTS) == 0, "parents 点要清掉"


def test_concurrent_same_name_versions_stay_consistent(rag_db: None) -> None:
    """连续两次同文档名导入（不同内容）→ 版本链收敛，最终只有一个 ready。"""
    from app.rag import store

    first = pipeline_import(SAMPLE, "合同.md")
    second = pipeline_import(SAMPLE.replace("三十日", "四十五日"), "合同.md")
    third = pipeline_import(SAMPLE.replace("三十日", "六十日"), "合同.md")

    assert str(store.get(first)["status"]) == "superseded"
    assert str(store.get(second)["status"]) == "superseded"
    assert str(store.get(third)["status"]) == "ready"

    # 同一时刻只有最新版本的 Qdrant 点 active
    # 旧版本的点允许存在（6h 窗口），但不能 active
    from app.core.vector import qdrant_store

    active = set()
    for page in qdrant_store.scroll_all("rag_chunks", with_vectors=False):
        for rec in page:
            pl = dict(rec.payload or {})
            if pl.get("status") == "active":
                active.add(int(pl.get("documents_id", -1)))
    assert active == {third}, f"只有最新版本 active，实际 {active}"


def pipeline_import(body: str, name: str) -> int:
    from app.rag.ingest import importer

    result = importer.import_bytes(body.encode(), name, embed=good_embed)
    assert result.status == "imported", result.reason
    return int(result.doc_id)  # type: ignore[arg-type]
