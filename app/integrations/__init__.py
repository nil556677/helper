"""框架适配层（净室 v2）：唯一允许 import langchain/langgraph 的包之一。

约定：
- `app/integrations` 可以 import `app/core`、`app/rag`、`app/services`；
- `app/core`、`app/ui` 不得 import 本包；
- 本包不 import `app/agent`（避免循环）。
"""

from __future__ import annotations

from app.integrations.embeddings import OnnxBgeM3Embeddings
from app.integrations.llm import chat_model
from app.integrations.qdrant import (
    COLLECTION,
    DENSE,
    SPARSE,
    build_stores,
    ensure_collection,
    qdrant_client,
    reset_collection,
)
from app.integrations.rerank import OnnxReranker
from app.integrations.sparse import JiebaBm25SparseEmbeddings, SparseTermStore

__all__ = [
    "COLLECTION",
    "DENSE",
    "SPARSE",
    "JiebaBm25SparseEmbeddings",
    "OnnxBgeM3Embeddings",
    "OnnxReranker",
    "SparseTermStore",
    "build_stores",
    "chat_model",
    "ensure_collection",
    "qdrant_client",
    "reset_collection",
]
