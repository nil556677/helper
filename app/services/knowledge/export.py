"""知识库的批量导出。"""

from __future__ import annotations

from pathlib import Path

from app.rag import kb_store


def export(doc_ids: list[int], target: Path) -> dict[str, object]:
    """**批量导出**（P1.17）：把选中的资料**副本 + 清单**打成一个 zip。

    只导出副本与元数据——**不导出向量、不导出库**（那是内部实现，搬出去也没用）。

    参数：
        doc_ids: 要导出的资料。
        target: zip 落盘路径。
    返回：``{"documents": n, "files": n, "path": str(target)}``。
    """
    import json
    import zipfile

    from app.core import paths

    ids = [int(doc_id) for doc_id in doc_ids]
    if not ids:
        return {"documents": 0, "files": 0, "path": str(target)}
    manifest: list[dict[str, object]] = []
    files = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for doc_id in ids:
            row = kb_store.get_document(doc_id)
            if row is None:
                continue
            stored = str(row["stored_path"] or "")
            copy = paths.resolve_stored_path(stored) if stored else None
            manifest.append(
                {
                    "id": doc_id,
                    "name": str(row["name"]),
                    "size": int(row["size"] or 0),
                    "pages": row["pages"],
                    "chars": None,
                    "file_hash": row["file_hash"],
                    "version": int(row["version"] or 1),
                    "exported_copy": bool(copy is not None and copy.is_file()),
                }
            )
            if copy is not None and copy.is_file():
                archive.write(copy, arcname=f"files/{doc_id}/{copy.name}")
                files += 1
        archive.writestr(
            "manifest.json",
            json.dumps({"documents": manifest}, ensure_ascii=False, indent=2),
        )
    return {"documents": len(manifest), "files": files, "path": str(target)}
