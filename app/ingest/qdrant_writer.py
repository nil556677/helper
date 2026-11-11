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
        from app.integrations.embeddings import OnnxBgeM3Embeddings  # 默认稠密嵌入器
        from app.integrations.sparse import JiebaBm25SparseEmbeddings  # 默认稀疏嵌入器

        self._embed = embedding or OnnxBgeM3Embeddings()  # 稠密
        self._sparse_embed = sparse_embedding or JiebaBm25SparseEmbeddings()  # 稀疏
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

    def add_multi_field(self, items: list[dict], ids: list[str]) -> None:
        """多字段 BM25 写：每 chunk 拆成 title/entity/kwd/content 字段，各建一个命名稀疏向量。

        绕过 ``language_chain QdrantVectorStore.add_documents``（它只支持单个 sparse 向量），
        直接用 ``client.upsert``：dense + 每字段稀疏（``s_*``）+ 空 legacy ``sparse``。
        """
        from qdrant_client import models  # PointStruct/命名向量

        from app.core.retrieval import terms as terms_mod  # 领域词（kwd 字段来源）
        from app.core.sparse import fields as kw_fields  # 字段名/命名向量
        from app.rag.normalize import normalize  # dense 归一（与嵌入侧同源）
        from app.rag.kw_fields import chunk_field_texts  # 字段抽取

        domain_terms = [word for word, _ in terms_mod.load_terms()]  # 领域词表
        points: list[Any] = []  # 待 upsert 的点
        for item, point_id in zip(items, ids):  # 逐 chunk
            meta = dict(item["metadata"])  # payload 的 metadata 段
            content = str(item.get("embed_text") or item["text"])  # 正文（嵌入文本）
            field_texts = chunk_field_texts(  # 每字段文本
                content=content,  # 正文
                title=str(meta.get("heading") or ""),  # 标题
                tags=list(meta.get("tags") or []),  # 标签
                domain_terms=domain_terms,  # 领域词
            )  # 结束
            field_vectors = self._sparse_embed.embed_multi_field(field_texts)  # {字段: sparse}
            dense = [_to_float(x) for x in self._embed.embed_query(normalize(content))]  # dense 向量
            vectors: dict[str, Any] = {qi.DENSE: dense}  # 向量表（dense 起手）
            vectors[qi.SPARSE] = models.SparseVector(indices=[], values=[])  # 兼容 legacy（空）
            for field in kw_fields.INDEX_FIELDS:  # 每个建索引字段
                vec = field_vectors.get(field)  # 该字段向量（langchain SparseVector）
                if vec is not None:  # 有向量
                    vectors[kw_fields.sparse_vector_name(field)] = models.SparseVector(  # 命名稀疏
                        indices=list(vec.indices),  # 词 id
                        values=list(vec.values),  # tf_norm
                    )  # 结束
                else:  # 空字段
                    vectors[kw_fields.sparse_vector_name(field)] = models.SparseVector(indices=[], values=[])  # 空
            points.append(  # 造点
                models.PointStruct(  # 点
                    id=str(point_id),  # id
                    vector=vectors,  # 命名向量（dense + 每字段 sparse + legacy）
                    payload={  # 与检索侧对齐：text 顶层 + metadata 嵌套（activate 用 metadata.doc_id）
                        "text": content,  # 正文（检索回显）
                        "metadata": meta,  # 元数据（嵌套，供 _version_filter / 检索取）
                    },  # payload 结束
                )  # 结束
            )  # 结束
        _guarded(  # 幂等 upsert，可重试
            lambda: self.client.upsert(  # 写
                collection_name=qi.COLLECTION,  # 集合
                points=points,  # 点
                wait=True,  # 等待生效
            )  # 写结束
        )  # 重试结束

    def _version_filter(self, doc_id: int, version: int) -> None:  # 版本过滤条件
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


def _to_float(value: Any) -> float:  # 转 float（向量元素）
    return float(value)  # 转