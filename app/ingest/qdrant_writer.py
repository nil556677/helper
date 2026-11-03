"""Qdrant 写入器：把 pipeline 的 child 文本/元数据写进单集合，并负责 activate/delete。

写、生效、删除都是**幂等**操作，发生瞬时错误（断连/限流/5xx）时用 `core.retry` 安全重试。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.core.retry import RetryPolicy, retry_sync  # 同步重试
from app.integrations import qdrant as qi  # Qdrant 适配

#: 写 Qdrant 的重试策略：只重试瞬时类（断连/限流/5xx），最多 3 次指数退避。
_RETRY = RetryPolicy(max_retries=3, base_delay_s=0.5, max_delay_s=4.0, jitter_s=0.1)  # 策略

#: 把「无参同步调用」包成带重试的（Qdrant 幂等操作安全）。
def _guarded(fn: Any) -> Any:  # 包重试
    return retry_sync(fn, _RETRY)  # 同步重试


class QdrantChunkWriter:
    def __init__(
        self,
        *,
        client: Any | None = None,
        embedding: Any | None = None,
        sparse_embedding: Any | None = None,
        dims: int = 768,
    ) -> None:
        self.client = client or qi.qdrant_client()  # 进程级客户端
        self.write_store, _, _ = qi.build_stores(  # 建写 store
            self.client,  # client
            embedding=embedding,  # 稠密嵌入器
            sparse_embedding=sparse_embedding,  # 稀疏嵌入器
            dims=dims,  # 维度
        )  # 建 store 结束

    def add(self, items: list[dict], ids: list[str], metas: list[dict]) -> None:
        del metas  # metadata 已在 docs 里
        from langchain_core.documents import Document  # 延迟导入

        docs = [  # 构 Document
            Document(  # 文档
                page_content=str(item.get("embed_text") or item["text"]),  # 嵌入文本
                metadata=dict(item["metadata"]),  # 元数据
            )  # 文档结束
            for item in items  # 逐条
        ]
        _guarded(  # 幂等 upsert，可重试
            lambda: self.write_store.add_documents(docs, ids=ids, batch_size=32)  # 写
        )  # 重试结束

    def _version_filter(self, doc_id: int, version: int):  # 版本过滤条件
        from qdrant_client import models  # 延迟导入

        return models.Filter(  # 过滤
            must=[  # 必须满足
                models.FieldCondition(  # 字段条件
                    key="metadata.doc_id", match=models.MatchValue(value=int(doc_id))  # 文档
                ),  # 条件结束
                models.FieldCondition(  # 字段条件
                    key="metadata.version", match=models.MatchValue(value=int(version))  # 版本
                ),  # 条件结束
            ]  # 必须结束
        )  # 返回

    def activate(self, doc_id: int, version: int) -> None:  # 生效
        # 顶层 status 是"可检索"开关；metadata 里的 status 只做记录
        _guarded(  # 幂等 set_payload，可重试
            lambda: self.client.set_payload(  # 设状态
                collection_name=qi.COLLECTION,  # 集合
                payload={"status": "active"},  # 可检索
                points=self._version_filter(doc_id, version),  # 版本定位
            )  # 设状态结束
        )  # 重试结束

    def delete_version(self, doc_id: int, version: int) -> None:  # 删版本
        from qdrant_client import models  # 延迟导入

        _guarded(  # 幂等 delete，可重试
            lambda: self.client.delete(  # 删
                collection_name=qi.COLLECTION,  # 集合
                points_selector=models.FilterSelector(  # 按过滤删
                    filter=self._version_filter(doc_id, version)  # 版本定位
                ),  # 删选择器结束
            )  # 删结束
        )  # 重试结束