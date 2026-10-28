"""RAG 检索纯函数：召回/融合/回填/精排/证据，不依赖图与 UI。"""

from __future__ import annotations  # 类型注解延迟求值

import os  # 读权重环境变量
from functools import lru_cache  # 复用 store
from typing import Any  # 任意值

from langchain_core.documents import Document  # 文档形状

from app.core.trace.recorder import trace_hit  # 命中留痕
from app.integrations.embeddings import OnnxBgeM3Embeddings  # 查询嵌入
from app.integrations.qdrant import build_stores, qdrant_client  # Qdrant
from app.integrations.rerank import OnnxReranker  # 精排
from app.integrations.retrievers import active_filter  # status 过滤
from app.integrations.sparse import JiebaBm25SparseEmbeddings  # 关键词
from app.rag.evidence import build_evidence, compose_candidates  # 证据

DENSE_CANDIDATES = 40  # 语义候选
SPARSE_CANDIDATES = 50  # 关键词候选
FUSED_CANDIDATES = 50  # 融合后保留
RRF_K = 60  # RRF 常数
RERANK_TOP = 10  # 精排保留
TOP_K = 6  # 最终证据


def _env_float(name: str, default: float) -> float:  # 读环境变量
    """坏值退回默认。"""
    try:  # 尝试解析
        return float(os.environ.get(name, default))  # 返回浮点
    except (TypeError, ValueError):  # 解析失败
        return default  # 默认


DENSE_WEIGHT = _env_float("HELPER_RRF_DENSE_WEIGHT", 1.0)  # 语义权重
SPARSE_WEIGHT = _env_float("HELPER_RRF_SPARSE_WEIGHT", 1.0)  # 关键词权重


@lru_cache(maxsize=1)
def _stores():  # 复用 store
    """返回 (dense, sparse, sparse_emb)；进程内只建一次。"""
    sparse_emb = JiebaBm25SparseEmbeddings()  # 稀疏编码
    client = qdrant_client()  # Qdrant 客户端
    _write, dense, sparse = build_stores(client, sparse_embedding=sparse_emb)  # 建 store
    return dense, sparse, sparse_emb  # 返回


def _doc_to_hit(doc: Document) -> dict[str, Any]:  # 文档转字典
    """保留正文与 metadata；score 已在 metadata。"""
    return {"text": doc.page_content, "metadata": dict(doc.metadata or {})}  # 字典


def _hit_to_doc(hit: dict[str, Any]) -> Document:  # 字典转文档
    """用 text + metadata 还原。"""
    return Document(page_content=str(hit.get("text") or ""), metadata=dict(hit.get("metadata") or {}))  # 文档


def embed_query(text: str) -> list[float]:  # 查询向量
    """bge-m3 单条嵌入。"""
    dense, _sparse, _sparse_emb = _stores()  # 取 store
    del dense, _sparse  # 只借嵌入器
    return [float(value) for value in OnnxBgeM3Embeddings().embed_query(text)]  # 转 list


def dense_search(vector: list[float], k: int = DENSE_CANDIDATES) -> list[dict[str, Any]]:  # 语义召回
    """按向量查 kb_chunks，写 dense 命中。"""
    dense, _sparse, _sparse_emb = _stores()  # 取 dense
    pairs = dense.similarity_search_with_score_by_vector(vector, k=k, filter=active_filter())  # 查询
    hits: list[dict[str, Any]] = []  # 结果
    for rank, (doc, score) in enumerate(pairs, 1):  # 逐条
        meta = dict(doc.metadata or {})  # 复制 metadata
        meta["score"] = float(score)  # 原始余弦
        meta["score_kind"] = "cosine"  # 分数语义
        hit = {"text": doc.page_content, "metadata": meta}  # 命中
        hits.append(hit)  # 追加
        trace_hit(  # 留痕
            stage="dense",  # 阶段
            rank=rank,  # 名次
            chunk_id=meta.get("chunk_id"),  # child id
            chunk_key=meta.get("chunk_key"),  # child key
            doc_id=meta.get("doc_id"),  # 文档 id
            source_name=meta.get("source_name", ""),  # 来源
            heading=meta.get("heading", ""),  # 标题
            loc=meta.get("loc", {}),  # 定位
            score=float(score),  # 分数
            score_kind="cosine",  # 语义
            text=doc.page_content,  # 正文
        )  # 留痕结束
    return hits  # 返回


def sparse_search(text: str, k: int = SPARSE_CANDIDATES) -> list[dict[str, Any]]:  # 关键词召回
    """BM25；查询无词直接返回空。"""
    _dense, sparse, sparse_emb = _stores()  # 取 sparse
    if not sparse_emb.has_terms(text):  # 没有可检索词
        return []  # 空
    pairs = sparse.similarity_search_with_score(text, k=k, filter=active_filter())  # 查询
    hits: list[dict[str, Any]] = []  # 结果
    for rank, (doc, score) in enumerate(pairs, 1):  # 逐条
        meta = dict(doc.metadata or {})  # 复制
        meta["score"] = float(score)  # BM25
        meta["score_kind"] = "bm25"  # 分数语义
        hits.append({"text": doc.page_content, "metadata": meta})  # 命中
        trace_hit(  # 留痕
            stage="sparse",  # 阶段
            rank=rank,  # 名次
            chunk_id=meta.get("chunk_id"),  # child id
            chunk_key=meta.get("chunk_key"),  # child key
            doc_id=meta.get("doc_id"),  # 文档 id
            source_name=meta.get("source_name", ""),  # 来源
            heading=meta.get("heading", ""),  # 标题
            loc=meta.get("loc", {}),  # 定位
            score=float(score),  # 分数
            score_kind="bm25",  # 关键词
            text=doc.page_content,  # 正文
        )  # 留痕结束
    return hits  # 返回


def fuse_hits(  # 融合
    dense_hits: list[dict[str, Any]],  # 语义
    sparse_hits: list[dict[str, Any]],  # 关键词
    *,
    top_k: int = FUSED_CANDIDATES,  # 保留数
) -> list[dict[str, Any]]:  # 结果
    """RRF 按名次融合，不跨路比原始分。"""
    scores: dict[str, float] = {}  # 名次分
    by_key: dict[str, dict[str, Any]] = {}  # key -> hit
    for rank, hit in enumerate(dense_hits, 1):  # 语义
        key = str((hit.get("metadata") or {}).get("chunk_key") or "")  # key
        if key:  # 有 key
            scores[key] = scores.get(key, 0.0) + DENSE_WEIGHT / (RRF_K + rank)  # 累加
            by_key.setdefault(key, hit)  # 保留
    for rank, hit in enumerate(sparse_hits, 1):  # 关键词
        key = str((hit.get("metadata") or {}).get("chunk_key") or "")  # key
        if key:  # 有 key
            scores[key] = scores.get(key, 0.0) + SPARSE_WEIGHT / (RRF_K + rank)  # 累加
            by_key.setdefault(key, hit)  # 保留
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]  # 排序截断
    fused: list[dict[str, Any]] = []  # 结果
    for key, score in ordered:  # 逐条
        hit = by_key.get(key)  # 取原文
        if hit is None:  # 防御
            continue  # 跳过
        meta = dict(hit.get("metadata") or {})  # 复制
        meta["score"] = float(score)  # RRF 分
        meta["score_kind"] = "rrf"  # 语义
        fused.append({"text": hit.get("text", ""), "metadata": meta})  # 追加
    for rank, hit in enumerate(fused, 1):  # 逐条留痕
        meta = hit.get("metadata") or {}  # metadata
        trace_hit(  # 留痕
            stage="fused",  # 阶段
            rank=rank,  # 名次
            chunk_id=meta.get("chunk_id"),  # child id
            chunk_key=meta.get("chunk_key"),  # child key
            doc_id=meta.get("doc_id"),  # 文档 id
            source_name=meta.get("source_name", ""),  # 来源
            heading=meta.get("heading", ""),  # 标题
            loc=meta.get("loc", {}),  # 定位
            score=float(meta.get("score") or 0.0),  # RRF
            score_kind="rrf",  # 融合
            text=str(hit.get("text") or ""),  # 正文
        )  # 留痕结束
    return fused  # 返回


def backfill(fused: list[dict[str, Any]]) -> list[dict[str, Any]]:  # 父块回填
    """按证据单位合并，取父块/整行文本。"""
    docs = [_hit_to_doc(hit) for hit in fused]  # 转文档
    candidates = compose_candidates(docs)  # 聚合
    return [_doc_to_hit(doc) for doc in candidates]  # 转回


def rerank(question: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:  # 精排
    """质量优先：默认执行；候选 <2 时压缩器自然短路。"""
    docs = [_hit_to_doc(hit) for hit in candidates]  # 转文档
    ranked = OnnxReranker(top_n=RERANK_TOP).compress_documents(docs, question)  # 精排
    return [_doc_to_hit(doc) for doc in ranked]  # 转回


def evidence(ranked: list[dict[str, Any]], top_k: int = TOP_K) -> list[dict[str, Any]]:  # 证据
    """去重/表格限流/截断，输出前端片段。"""
    docs = [_hit_to_doc(hit) for hit in ranked]  # 转文档
    return build_evidence(docs, top_k=top_k)  # 组装

