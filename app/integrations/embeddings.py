"""ONNX bge-m3 嵌入的 LangChain 适配。

必须保留 `core/embed` 的三条不变量（见 core/embed/model.py）：
固定宽度 PAD_TO=320、每批至少 2 行、入库按 16 行批。
"""

from __future__ import annotations

from langchain_core.embeddings import Embeddings

from app.core.embed import DEFAULT_BATCH_SIZE, embed_text, embed_texts


class OnnxBgeM3Embeddings(Embeddings):
    """把本地 ONNX 嵌入暴露成 LangChain 的 ``Embeddings`` 接口。

    禁止用 ``HuggingFaceEmbeddings`` 替代：pooling 路径不同，向量与库不兼容。
    """

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), DEFAULT_BATCH_SIZE):
            batch = texts[start : start + DEFAULT_BATCH_SIZE]
            out.extend(
                [[float(x) for x in row] for row in embed_texts(batch)]
            )
        return out

    def embed_query(self, text: str) -> list[float]:
        return [float(x) for x in embed_text(text)]
