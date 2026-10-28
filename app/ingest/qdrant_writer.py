"""Qdrant 写入器：把 pipeline 的 child 文本/元数据写进单集合，并负责 activate/delete。"""

from __future__ import annotations

from typing import Any

from app.integrations import qdrant as qi


class QdrantChunkWriter:
    def __init__(
        self,
        *,
        client: Any | None = None,
        embedding: Any | None = None,
        sparse_embedding: Any | None = None,
        dims: int = 768,
    ) -> None:
        self.client = client or qi.qdrant_client()
        self.write_store, _, _ = qi.build_stores(
            self.client,
            embedding=embedding,
            sparse_embedding=sparse_embedding,
            dims=dims,
        )

    def add(self, items: list[dict], ids: list[str], metas: list[dict]) -> None:
        del metas
        from langchain_core.documents import Document

        docs = [
            Document(
                page_content=str(item.get("embed_text") or item["text"]),
                metadata=dict(item["metadata"]),
            )
            for item in items
        ]
        self.write_store.add_documents(docs, ids=ids, batch_size=32)

    def _version_filter(self, doc_id: int, version: int):
        from qdrant_client import models

        return models.Filter(
            must=[
                models.FieldCondition(
                    key="metadata.doc_id", match=models.MatchValue(value=int(doc_id))
                ),
                models.FieldCondition(
                    key="metadata.version", match=models.MatchValue(value=int(version))
                ),
            ]
        )

    def activate(self, doc_id: int, version: int) -> None:
        # 顶层 status 是"可检索"开关；metadata 里的 status 只做记录
        self.client.set_payload(
            collection_name=qi.COLLECTION,
            payload={"status": "active"},
            points=self._version_filter(doc_id, version),
        )

    def delete_version(self, doc_id: int, version: int) -> None:
        from qdrant_client import models

        self.client.delete(
            collection_name=qi.COLLECTION,
            points_selector=models.FilterSelector(
                filter=self._version_filter(doc_id, version)
            ),
        )
