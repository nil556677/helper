"""BM25 编码器单测（纯函数）。"""

from __future__ import annotations

from app.core.sparse.bm25 import encode_document, encode_query, idf, tf_norm


def test_tf_norm_baseline_is_one() -> None:
    # tf=1, dl=avgdl → 1.0
    assert abs(tf_norm(1, 10, 10.0) - 1.0) < 1e-9


def test_tf_norm_saturates() -> None:
    one = tf_norm(1, 10, 10.0)
    ten = tf_norm(10, 10, 10.0)
    assert ten > one
    assert ten < 10 * one  # 饱和：不会线性放大


def test_tf_norm_zero_avgdl_returns_zero() -> None:
    assert tf_norm(3, 10, 0.0) == 0.0


def test_idf_monotonic() -> None:
    assert idf(100, 1) > idf(100, 10) > 0.0


def test_encode_document_empty_or_unknown_returns_none() -> None:
    assert encode_document(tokens=[], term_id_of=lambda _t: 1, avgdl=5.0) is None
    assert encode_document(tokens=["zzz"], term_id_of=lambda _t: None, avgdl=5.0) is None


def test_encode_document_sorted_by_term_id_and_keeps_tf() -> None:
    term_id_of = {"a": 3, "b": 1}
    sv = encode_document(tokens=["a", "b", "a"], term_id_of=term_id_of.get, avgdl=10.0)
    assert sv is not None
    assert sv.indices == [1, 3]  # 按 term_id 升序
    # a 词频 2 → 值 > b 词频 1
    assert sv.values[1] > sv.values[0]


def test_encode_query_dedupes_and_uses_idf() -> None:
    stats = {"a": (3, 1), "b": (1, 10)}
    sv = encode_query(tokens=["a", "a", "b"], term_stats=stats, n=100)
    assert sv is not None
    assert sv.indices == [1, 3]  # sorted by term id
    assert sv.indices.count(3) == 1  # 去重：a 只算一次
    assert abs(sv.values[1] - idf(100, 1)) < 1e-9


def test_encode_query_unknown_terms_skipped() -> None:
    assert encode_query(tokens=["nope"], term_stats={}, n=100) is None


def test_encode_document_uses_given_k1_b() -> None:
    term_id_of = {"a": 1}
    default = encode_document(tokens=["a"], term_id_of=term_id_of.get, avgdl=10.0)
    custom = encode_document(
        tokens=["a"], term_id_of=term_id_of.get, avgdl=10.0, k1=2.0, b=0.0
    )
    assert default is not None and custom is not None
    assert default.values[0] != custom.values[0]


def test_document_terms_capped() -> None:
    term_id_of = {f"t{i}": i for i in range(300)}
    sv = encode_document(tokens=[f"t{i}" for i in range(300)], term_id_of=term_id_of.get, avgdl=50.0)
    assert sv is not None
    assert len(sv.indices) <= 256
