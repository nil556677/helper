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
            # 已按降序：一旦低于闸门，后面的只会更低 → 直接停（等价于原 continue 但更早退出）
            if self.min_score is not None and value < self.min_score:
                break
            doc.metadata = {**(doc.metadata or {}), "rerank_score": value}
            kept.append(doc)
            if len(kept) >= self.top_n:
                break
        # 保底：非空输入绝不压成空表——否则所有候选都低于闸门时，下游 gate 拿到「空 ranked」
        # 会直接 no_evidence/reject，明明库里有资料也「搜到了却什么都没给」（4b 实测根因）。
        # 全低分时保留最高分那一条（带上 rerank_score），交由门限判 weak/pass，而不是凭空消失。
        if not kept and ranked:
            best_doc, best_score = ranked[0]  # 降序第一个 = 最高分
            best_doc.metadata = {**(best_doc.metadata or {}), "rerank_score": float(best_score)}
            kept = [best_doc]
        return kept
