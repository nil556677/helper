"""BM25 编码（混合搜索数据面）：文档侧存 tf 归一化，查询侧乘 IDF。

Qdrant sparse 点积 = ``Σ idf(t) * tf_norm(t,d)`` 正好是 BM25（查询词 qtf=1，去重）。

- 文档侧只用 ``tf_norm``（依赖文档自身 + 建库时的 ``avgdl``），不依赖 df/N；
- 查询侧用当前 ``N/df`` 算 ``idf``——新增文档不重算旧 sparse 值。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

#: BM25 常数（与计划一致，改动必须带评测）。
K1 = 1.2
B = 0.75

#: 单文档/单查询的术语数上限（防超长文本撑大 sparse 向量）。
MAX_TERMS_PER_DOC = 256
MAX_TERMS_PER_QUERY = 64


@dataclass(frozen=True)
class SparseVectorData:
    """Qdrant SparseVector 的 (indices, values)。"""

    indices: list[int]
    values: list[float]

    def to_qdrant(self):
        from qdrant_client import models

        return models.SparseVector(indices=self.indices, values=self.values)


def tf_norm(tf: int, dl: int, avgdl: float, *, k1: float = K1, b: float = B) -> float:
    """BM25 词频饱和 + 长度归一。"""
    if dl <= 0 or avgdl <= 0:
        return 0.0
    return tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))


def idf(n: int, df: int) -> float:
    """查询侧 IDF。"""
    return math.log(1 + (n - df + 0.5) / (df + 0.5))


def _content_tokens(tokens: list[str]) -> list[str]:
    """去掉纯标点/符号（它们 df 很高、对检索没意义）。"""
    return [
        token
        for token in tokens
        if any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in token)
    ]


def encode_document(
    *,
    tokens: list[str],
    term_id_of: Callable[[str], int | None],
    avgdl: float,
    k1: float = K1,
    b: float = B,
) -> SparseVectorData | None:
    """文档 sparse：每个词 = tf_norm（不含 IDF）。空/OOV 全丢返回 None。"""
    counts = Counter(_content_tokens(tokens))
    if not counts:
        return None
    total = sum(counts.values())
    items: list[tuple[int, float]] = []
    for term, tf in counts.most_common(MAX_TERMS_PER_DOC):
        tid = term_id_of(term)
        if tid is None:
            continue
        items.append((tid, tf_norm(int(tf), total, avgdl, k1=k1, b=b)))
    if not items:
        return None
    items.sort(key=lambda pair: pair[0])
    return SparseVectorData(
        indices=[index for index, _ in items],
        values=[value for _, value in items],
    )


def encode_query(
    *,
    tokens: list[str],
    term_stats: dict[str, tuple[int, int]],
    n: int,
) -> SparseVectorData | None:
    """查询 sparse：去重后的每个词 = IDF（qtf=1）。"""
    items: list[tuple[int, float]] = []
    for term in dict.fromkeys(_content_tokens(tokens)):
        info = term_stats.get(term)
        if info is None:
            continue
        tid, df = info
        items.append((tid, idf(n, df)))
        if len(items) >= MAX_TERMS_PER_QUERY:
            break
    if not items:
        return None
    items.sort(key=lambda pair: pair[0])
    return SparseVectorData(
        indices=[index for index, _ in items],
        values=[value for _, value in items],
    )
