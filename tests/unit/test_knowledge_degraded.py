"""概览降级：向量后端不可用时 stats() 不抛，返回 vector_error 供界面显示"暂不可用"。"""

from __future__ import annotations


def test_stats_degrades_when_qdrant_unavailable(rag_db: None, monkeypatch) -> None:
    from app.services import knowledge

    def boom(*args, **kwargs):
        raise RuntimeError("Qdrant 不可用")

    monkeypatch.setattr("app.core.vector.qdrant_store.client", boom)

    data = knowledge.stats()  # 不应抛
    assert data.vector_error, "应带不可用原因"
    assert data.vector_bytes == 0
    assert data.chunks == 0  # counts() 内部已兜底
    assert isinstance(data.documents, int)


def test_stats_normal_has_no_error(rag_db: None) -> None:
    from app.services import knowledge

    data = knowledge.stats()
    assert data.vector_error == ""
