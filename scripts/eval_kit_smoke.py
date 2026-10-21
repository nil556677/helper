"""eval-kit 端到端冒烟：逐个格式导入 → 详情/片段 → 更新 → 删除。

用假嵌入（不加载真实模型），在隔离数据目录里跑，输出每格式结果表。
用法：python scripts/eval_kit_smoke.py
"""

from __future__ import annotations

import os
import pathlib
import time
from collections.abc import Sequence

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
KIT = ROOT / "data" / "rag-test" / "eval-kit"


def fake_embed(texts: Sequence[str]) -> np.ndarray:
    from app.core.config import store as params_store

    size = int(params_store.load()["embed.dims"])
    rows = []
    for text in texts:
        v = np.zeros(size, dtype=np.float32)
        v[abs(hash(text)) % size] = 1.0
        rows.append(v)
    return np.stack(rows)


def main() -> int:
    # 隔离数据目录（不碰真实 data/）；避免 tempfile.mkdtemp 的 0o700 目录（受限环境不可写）
    tmp = ROOT / ".tmp" / f"evalkit-{os.getpid()}"
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    from app.core import paths

    paths.project_root = lambda: tmp
    from app.core.db.schema import init_db

    init_db()

    from app.rag import store
    from app.rag.ingest import importer
    from app.services import knowledge as knowledge_service

    rows: list[dict] = []
    for sub in sorted(p for p in KIT.iterdir() if p.is_dir()):
        doc = next((p for p in sub.iterdir() if p.name.startswith("doc.")), None)
        if doc is None:
            continue
        name = sub.name
        started = time.time()
        try:
            result = importer.import_path(doc, doc.name, embed=fake_embed)
            status = result.status
            detail_id = int(result.doc_id) if result.doc_id else None
        except Exception as exc:  # 解析器可能抛（不支持的格式等）
            status = f"exception:{type(exc).__name__}"
            detail_id = None
        ms = int((time.time() - started) * 1000)

        detail_ok = chunks_ok = "—"
        if detail_id:
            try:
                det = knowledge_service.document_detail(detail_id)
                detail_ok = f"children={det['children']} parents={det['parents']}"
            except Exception as exc:
                detail_ok = f"err:{exc}"
            try:
                ch = knowledge_service.document_chunks(detail_id, page=1, size=10)
                chunks_ok = f"total={ch['total']}"
            except Exception as exc:
                chunks_ok = f"err:{exc}"

        row = {
            "format": name,
            "file": doc.name,
            "status": status,
            "ms": ms,
            "detail": detail_ok,
            "chunks": chunks_ok,
            "update": "—",
            "delete": "—",
        }
        # 更新：同文件名再导入一份不同内容 → 新版本（只在成功时做）
        if detail_id and status == "imported":
            try:
                second = importer.import_path(doc, doc.name, embed=fake_embed)
                row["update"] = f"v2={second.status}"
            except Exception as exc:
                row["update"] = f"err:{type(exc).__name__}"
        # 删除：清 Qdrant + 行（成功时做，用最初的 detail_id）
        if detail_id:
            try:
                before_chunks = store.counts().get("chunks", 0)
                importer.delete_documents([detail_id])
                from app.core.vector import api

                after = api.count(api.COLLECTION_CHUNKS)
                row["delete"] = f"ok(chunks {before_chunks}->{after})"
            except Exception as exc:
                row["delete"] = f"err:{type(exc).__name__}"
        rows.append(row)

    print(f"{'format':10} {'status':14} {'ms':>5} {'detail':28} {'chunks':14} {'update':10} {'delete':18}")
    for r in rows:
        print(
            f"{r['format']:10} {r['status']!s:14} {r['ms']:>5} {r['detail']!s:28} "
            f"{r['chunks']!s:14} {r['update']!s:10} {r['delete']!s:18}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
