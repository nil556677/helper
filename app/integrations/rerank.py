"""ONNX bge-reranker 的 LangChain 压缩器适配。"""

from __future__ import annotations

from langchain_core.documents import Document
from langchain_core.documents.compressor import BaseDocumentCompressor

from app.core.rerank import is_worth_reranking, score_pairs

#: 相关性闸门（reranker logit）。本机 7 份语料实测：相关查询 top 分 +2.6~+6.1，
#: 无关查询 -4.5~-6.9；取 -1.0 作为“无命中”边界（换模型必须重标）。
MIN_SCORE = -1.0


class OnnxReranker(BaseDocumentCompressor):
    """本地精排：候选 <2 或失败/超时 → 按原序截断；低于 MIN_SCORE 的丢掉。"""

    top_n: int = 10
    min_score: float | None = MIN_SCORE

    def compress_documents(
        self,
        documents: list[Document],
        query: str,
        callbacks=None,
    ) -> list[Document]:
        del callbacks
        if not documents or not is_worth_reranking(len(documents)):
            return documents[: self.top_n]
        try:
            scores = score_pairs(query, [doc.page_content for doc in documents])
        except Exception:
            return documents[: self.top_n]
        ranked = sorted(
            zip(documents, scores, strict=False),
            key=lambda pair: float(pair[1]),
            reverse=True,
        )
        kept: list[Document] = []
        for doc, score in ranked:
            value = float(score)
            if self.min_score is not None and value < self.min_score:
                continue
            doc.metadata = {**(doc.metadata or {}), "rerank_score": value}
            kept.append(doc)
            if len(kept) >= self.top_n:
                break
        return kept
