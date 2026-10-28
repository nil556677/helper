"""jieba + BM25 的 sparse 编码（LangChain ``SparseEmbeddings`` 接口）。

- term → uint32 id、df、N/avgdl 都存 SQLite（`sparse_terms` / `sparse_meta`）；
- 文档侧存 ``tf_norm``（依赖 avgdl），查询侧存 ``IDF``（依赖 N/df）；
- 增量导入会带来 avgdl 轻微漂移；删除/重建后调用 :meth:`SparseTermStore.recompute`。
"""

from __future__ import annotations

import time
from collections import Counter

from langchain_qdrant import SparseEmbeddings, SparseVector

from app.core.db.connection import get_connection, transaction
from app.core.retrieval.tokenize import to_tokens
from app.core.sparse.bm25 import encode_document, encode_query

_KEY_N = "N"
_KEY_TOTAL_LEN = "total_len"
_KEY_AVGDL = "avgdl"
_KEY_BUILT_AT = "built_at"


class SparseTermStore:
    """sparse 词项与统计的 SQLite 读写（v1 表）。"""

    def term_id(self, term: str) -> int | None:
        if not term:
            return None
        conn = get_connection()
        row = conn.execute(
            "SELECT term_id FROM sparse_terms WHERE term = ?", (term,)
        ).fetchone()
        if row is not None:
            return int(row["term_id"])
        with transaction() as tx:
            row = tx.execute(
                "SELECT term_id FROM sparse_terms WHERE term = ?", (term,)
            ).fetchone()
            if row is not None:
                return int(row["term_id"])
            nid = int(
                tx.execute(
                    "SELECT COALESCE(MAX(term_id), 0) + 1 AS nid FROM sparse_terms"
                ).fetchone()["nid"]
            )
            tx.execute(
                "INSERT INTO sparse_terms(term, term_id, df, updated_at) VALUES (?, ?, 0, ?)",
                (term, nid, time.time()),
            )
            return nid

    def term_stats(self, terms: list[str]) -> dict[str, tuple[int, int]]:
        if not terms:
            return {}
        conn = get_connection()
        placeholders = ", ".join("?" * len(terms))
        rows = conn.execute(
            f"SELECT term, term_id, df FROM sparse_terms WHERE term IN ({placeholders})",
            terms,
        ).fetchall()
        return {
            str(row["term"]): (int(row["term_id"]), int(row["df"])) for row in rows
        }

    def n(self) -> int:
        try:
            return int(self._meta(_KEY_N) or "0")
        except (TypeError, ValueError):
            return 0

    def avgdl(self) -> float:
        try:
            return float(self._meta(_KEY_AVGDL) or "0")
        except (TypeError, ValueError):
            return 0.0

    def _meta(self, key: str) -> str | None:
        row = get_connection().execute(
            "SELECT value FROM sparse_meta WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def register_documents(self, tokens_list: list[list[str]]) -> None:
        """一批文档登记：分配 term_id、df+1、更新 N/total_len/avgdl。"""
        if not tokens_list:
            return
        with transaction() as tx:
            counts: Counter = Counter()
            total_len = 0
            for items in tokens_list:
                unique = set(items)
                total_len += len(items)
                for term in unique:
                    counts[term] += 1
            for term in counts:
                row = tx.execute(
                    "SELECT term_id FROM sparse_terms WHERE term = ?", (term,)
                ).fetchone()
                if row is not None:
                    tid = int(row["term_id"])
                else:
                    nid = int(
                        tx.execute(
                            "SELECT COALESCE(MAX(term_id), 0) + 1 AS nid FROM sparse_terms"
                        ).fetchone()["nid"]
                    )
                    tx.execute(
                        "INSERT INTO sparse_terms(term, term_id, df, updated_at)"
                        " VALUES (?, ?, 0, ?)",
                        (term, nid, time.time()),
                    )
                    tid = nid
                tx.execute(
                    "UPDATE sparse_terms SET df = df + ? WHERE term_id = ?",
                    (int(counts[term]), tid),
                )
            old_n = int(self._meta(_KEY_N) or "0")
            old_len = int(self._meta(_KEY_TOTAL_LEN) or "0")
            new_n = old_n + len(tokens_list)
            new_len = old_len + total_len
            avgdl = new_len / new_n if new_n else 0.0
            stamp = time.time()
            for key, value in (
                (_KEY_N, new_n),
                (_KEY_TOTAL_LEN, new_len),
                (_KEY_AVGDL, avgdl),
                (_KEY_BUILT_AT, stamp),
            ):
                tx.execute(
                    "INSERT INTO sparse_meta(key, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at",
                    (key, str(value), stamp),
                )

    def reset(self) -> None:
        """清空词项与统计（重建 sparse 前调用；term_id 会重新分配）。"""
        with transaction() as tx:
            tx.execute("DELETE FROM sparse_terms")
            tx.execute("DELETE FROM sparse_meta")

    def recompute(self, token_sets: list[list[str]]) -> None:
        """全量重建统计（删除/重建后校正 avgdl/df）。token_sets 是所有 active 文档的分词。"""
        self.reset()
        self.register_documents(token_sets)


class JiebaBm25SparseEmbeddings(SparseEmbeddings):
    """jieba 分词 + BM25；term_id/df/N 存 SQLite。"""

    def __init__(self, store: SparseTermStore | None = None) -> None:
        self._store = store or SparseTermStore()

    def embed_documents(self, texts: list[str]) -> list[SparseVector]:
        tokens = [to_tokens(text).split() for text in texts]
        self._store.register_documents(tokens)
        avgdl = self._store.avgdl()
        out: list[SparseVector] = []
        for items in tokens:
            data = encode_document(
                tokens=items,
                term_id_of=self._store.term_id,
                avgdl=avgdl,
            )
            out.append(
                SparseVector(
                    indices=data.indices if data else [],
                    values=data.values if data else [],
                )
            )
        return out

    def embed_query(self, text: str) -> SparseVector:
        tokens = to_tokens(text).split()
        data = encode_query(
            tokens=tokens,
            term_stats=self._store.term_stats(tokens),
            n=self._store.n(),
        )
        return SparseVector(
            indices=data.indices if data else [],
            values=data.values if data else [],
        )

    def has_terms(self, text: str) -> bool:
        """查询里是否还有可检索的词（空 sparse 时该路返回 0 条，不去查 Qdrant）。"""
        tokens = to_tokens(text).split()
        return any(token in self._store.term_stats(tokens) for token in tokens)
