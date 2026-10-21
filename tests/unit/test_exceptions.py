"""异常角度测试：空文件/损坏/畸形输入/Qdrant 不可用/维度不符/取消传播。

目标：出错要"可读、可分类、可恢复"，不崩界面、不吞错误、不留半写。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pytest


def good_embed(texts: Sequence[str]) -> np.ndarray:
    from app.core.config import store as params_store

    size = int(params_store.load()["embed.dims"])
    rows = []
    for text in texts:
        v = np.zeros(size, dtype=np.float32)
        v[abs(hash(text)) % size] = 1.0
        rows.append(v)
    return np.stack(rows)


def test_empty_file_is_reported_not_crash(ingest_db: None) -> None:
    from app.rag.ingest import importer

    result = importer.import_bytes(b"", "空.md", embed=good_embed)
    assert result.status == "failed"
    assert result.error_kind in ("bad_request", "not_found")


def test_corrupt_pdf_is_reported_not_crash(rag_db: None, tmp_path) -> None:
    from app.rag.ingest import importer

    bad = tmp_path / "坏.pdf"
    bad.write_bytes(b"%PDF-1.4 this is not a real pdf at all" * 5)
    result = importer.import_path(bad, "坏.pdf", embed=good_embed)
    assert result.status == "failed"
    assert result.error_kind, "应有 error_kind（bad_request/unsupported）"
    assert store_has_failed_row(result)


def store_has_failed_row(result) -> bool:
    from app.rag import store

    if not result.doc_id:
        return False
    row = store.get(int(result.doc_id))
    return row is not None and str(row["status"]) == "failed"


def test_hostile_text_attrs_do_not_crash(ingest_db: None) -> None:
    """畸形/超长文本进 attrs 提取：不抛、不吞（返回空或部分结果）。"""
    from app.core.attrs import build_attrs

    hostile = "第" * 5000 + "条" * 5000 + "人民币" * 5000 + "\x00\x01\x02" + "甲方：" * 3000
    attrs = build_attrs(hostile)  # 不应抛异常
    assert isinstance(attrs, dict)


def test_vector_session_wrong_dims_raises_vector_error(rag_db: None) -> None:
    from app.core.vector import api

    api.ensure("rag_chunks")
    with pytest.raises(api.VectorError), api.VectorSession("rag_chunks") as session:
        session.add(
            [
                {
                    "key": "1:1:0",
                    "vector": np.zeros(api.dims() + 7, dtype=np.float32),
                    "payload": {},
                }
            ]
        )


def test_delete_nonexistent_document_is_noop(rag_db: None) -> None:
    from app.rag.ingest import importer

    assert importer.delete_documents([99999]) == 0  # 不崩、返回 0


def test_counts_survives_qdrant_missing(rag_db: None, monkeypatch) -> None:
    from app.rag import store

    def boom(*a, **k):
        raise RuntimeError("Qdrant 不可用")

    monkeypatch.setattr("app.core.vector.api.count", boom)
    counts = store.counts()
    assert counts["chunks"] == 0, "Qdrant 不可用时概览给 0，不崩"


def test_ingest_cancel_is_job_interrupted_not_failure(rag_db: None) -> None:
    """取消要传播为 JobInterrupted，而不是被当成普通失败/重试。"""
    from app.core.jobs.model import JobInterrupted
    from app.rag.ingest import importer

    def cancel_embed(texts: Sequence[str]) -> np.ndarray:
        raise JobInterrupted("用户取消")

    with pytest.raises(JobInterrupted):
        importer.import_bytes(("# 标题\n\n" + "内容。" * 400).encode(), "取消.md", embed=cancel_embed)


def test_vector_session_skips_flush_on_exception(rag_db: None) -> None:
    """add 抛 VectorError 时 __exit__ 不重写失败批次、不掩盖原始异常（异常审查）。"""
    from app.core.vector import api

    api.ensure("rag_chunks")
    with pytest.raises(api.VectorError) as info, api.VectorSession("rag_chunks") as session:
        session.add(
            [{"key": "1:1:0", "vector": np.zeros(5, dtype=np.float32), "payload": {}}]
        )
    assert "维度" in str(info.value) or "dims" in str(info.value).lower() or "incompatible" in str(info.value).lower()


def test_detail_and_chunks_guard_qdrant_down(rag_db: None, monkeypatch) -> None:
    """Qdrant 不可用时详情/片段返回降级空值，不崩页（异常审查）。"""
    from app.rag.ingest import importer
    from app.services import knowledge as knowledge_service

    doc_id = int(importer.import_bytes(SAMPLE.encode(), "g.md", embed=good_embed).doc_id)

    def boom(*a, **k):
        raise RuntimeError("Qdrant 不可用")

    monkeypatch.setattr("app.core.vector.qdrant_store.client", boom)

    det = knowledge_service.document_detail(doc_id)
    assert det is not None
    assert det["children"] == 0 and det["parents"] == 0
    ch = knowledge_service.document_chunks(doc_id)
    assert ch["total"] == 0


def test_reconcile_guards_qdrant_down(rag_db: None, monkeypatch) -> None:
    """对账在 Qdrant 不可用时返回错误标记而非崩（异常审查）。"""
    from app.services import maintenance

    def boom(*a, **k):
        raise RuntimeError("Qdrant 不可用")

    monkeypatch.setattr("app.core.vector.qdrant_store.client", boom)
    out = maintenance.reconcile()
    assert "error" in out


def test_reconcile_error_keeps_full_counts_shape(monkeypatch) -> None:
    """对账失败时 ``counts`` 也要是**完整形状**（四个键全在、值全 0）。

    真实踩过：启动早期 Qdrant 未就绪 → 异常路径返回 ``counts={}`` → cleanup 任务取
    ``counts['vector_orphans']`` 直接 KeyError，用户看到"清理存储"失败。
    """
    from app.services import maintenance

    def boom(*_a, **_k):
        raise RuntimeError("Qdrant 不可用")

    monkeypatch.setattr("app.rag.store.live_child_keys", boom)
    result = maintenance.reconcile()

    assert result.get("error")
    assert set(result["counts"]) == {
        "vector_orphans",
        "vector_missing",
        "documents_without_chunks",
        "orphan_copies",
    }
    assert all(value == 0 for value in result["counts"].values())




def test_ingest_failure_kind_controls_retryable() -> None:
    """内容问题不可重试、基础设施问题可重试（异常审查）。"""
    from app.tasks.ingest import _failure

    class R:
        def __init__(self, status, reason, error_kind):
            self.status = status
            self.reason = reason
            self.error_kind = error_kind
            self.doc_id = None
            self.notes = []

    err = _failure(R("failed", "格式不支持", "unsupported"))
    assert err.retryable is False
    err2 = _failure(R("failed", "写副本失败", "io"))
    assert err2.retryable is True


def test_attrs_skip_malformed_chinese_numbers() -> None:
    """畸形中文数字不抛、被跳过（异常审查）。"""
    from app.core.attrs import build_attrs
    from app.core.attrs.extractors import extract_amounts, extract_clause_nos

    text = "价格人民币一百零（畸形） 人民币十百（畸形） 第三条 第零条 正常金额人民币壹佰万元整"
    # 不抛即可
    extract_amounts(text)
    extract_clause_nos(text)
    attrs = build_attrs(text)
    assert isinstance(attrs, dict)


SAMPLE = "# 采购合同\n\n第一条 乙方应当交付设备。\n"
