"""彻底修复的回归：Qdrant 并发、删除/取消顺序、占位行、语义兜底护栏、取消 toast。"""

from __future__ import annotations

import pytest


def test_vector_progress_degrades_instead_of_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """向量库读取失败时进度按 0 返回，异常不能冒到页面构建（防 500）。"""
    from app.core.vector import qdrant_store
    from app.rag.store import stats

    stats.clear_caches()

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated qdrant race")

    monkeypatch.setattr(qdrant_store, "scroll_filter", boom)
    try:
        assert stats.vector_progress_by_doc([7]) == {7: (0, 0)}
    finally:
        stats.clear_caches()


def test_rebuild_requires_a_copy(rag_db: None) -> None:
    """pending 占位行没有副本：重建入口提前拒绝，不排一条注定失败的任务。"""
    from app.rag import store
    from app.services.knowledge.queue import enqueue_rebuild

    doc_id = store.insert_pending("x.md", size=1, file_hash="f" * 64)
    with pytest.raises(ValueError, match="副本"):
        enqueue_rebuild(doc_id)


def test_delete_documents_cancels_related_jobs(rag_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """删除资料前先取消引用它的任务（解决"先删资料/先取消"两种顺序）。"""
    from app.core.jobs import api as jobs_api
    from app.rag import store
    from app.rag.ingest import importer

    cancelled: list[int] = []

    class _Job:
        id = 55

        def __init__(self) -> None:
            self.payload = {"doc_id": 9}

    monkeypatch.setattr(jobs_api, "list_jobs", lambda **_kwargs: [_Job()])
    monkeypatch.setattr(jobs_api, "cancel", lambda job_id: cancelled.append(int(job_id)) or True)
    monkeypatch.setattr(store, "delete_chunks", lambda _conn, _doc_id: None)
    monkeypatch.setattr(store, "delete_many", lambda _ids: [])
    monkeypatch.setattr(store, "paths_in_use", lambda: set())
    monkeypatch.setattr(store, "clear_rerank_cache", lambda: 0)

    importer.delete_documents([9])
    assert cancelled == [55]


def test_cancel_unstarted_import_cleans_placeholder(rag_db: None) -> None:
    """取消"还没开始"的导入：占位行要被清掉，否则重建必失败、重导又被判重复。"""
    from types import SimpleNamespace

    from app.rag import store
    from app.services import tasks as tasks_service

    doc_id = store.insert_pending("x.xlsx", size=1, file_hash="b" * 64)
    job = SimpleNamespace(payload={"doc_id": doc_id, "path": ""})
    tasks_service._cleanup_unstarted_import(job)
    assert store.get(doc_id) is None


def test_stream_into_aborts_when_document_was_deleted() -> None:
    """写入前确认资料行还在；已被删除就抛 JobCancelled，不写孤儿点。"""
    from app.core.jobs.model import JobCancelled
    from app.rag.ingest.streaming import _ensure_document_alive

    with pytest.raises(JobCancelled):
        _ensure_document_alive(999_999)


def test_bootstrap_config_roundtrip_in_database(ingest_db: None) -> None:
    """引导配置存 settings 表，不再生成 config.json。"""
    from app.core.config import access
    from app.core.config.schema import BootstrapConfig

    access.invalidate()
    try:
        access.save(BootstrapConfig(log_level="DEBUG", log_debug=True, host="127.0.0.1"))
        config = access.load()
        assert config.log_level == "DEBUG"
        assert config.log_debug is True
        assert not access.config_file().exists()
    finally:
        access.save(BootstrapConfig())


def test_config_json_is_no_longer_used(fake_root: object, ingest_db: None) -> None:
    """config.json 已废弃：ensure_default 写库默认值，不读取、不迁移、不归档旧文件。"""
    import json
    from pathlib import Path

    from app.core.config import access

    root = Path(str(fake_root))
    legacy = root / "config.json"
    legacy.write_text(
        json.dumps({"log_level": "DEBUG", "log_debug": True, "host": "127.0.0.1"}),
        encoding="utf-8",
    )
    access.invalidate()
    access.ensure_default()

    config = access.load()
    assert config.log_debug is False, "config.json 不再被读取，应保持默认值"
    assert legacy.exists(), "旧 config.json 不再被迁移，原样保留"
    assert not list(root.glob("config.migrated-*.json")), "不再产生已迁移归档文件"


def test_enable_server_requires_qdrant_binary(
    ingest_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没装 qdrant.exe 时不允许切服务端（避免把 UI 指向一个不存在的服务）。"""
    from pathlib import Path

    from app.core.proc import services

    monkeypatch.setattr(services, "qdrant_binary", lambda: Path("missing-qdrant.exe"))
    result = services.enable_server(6333)
    assert result["ok"] is False
    assert "bin/qdrant" in str(result["message"])


def test_finish_document_reports_zero_after_delete(rag_db: None) -> None:
    """揭示事务必须能看出资料行已被删除（0 行），否则会留下 active 孤儿点。"""
    from app.core.db.connection import transaction
    from app.rag import store

    doc_id = store.insert_pending("x.md", size=1, file_hash="c" * 64)
    with transaction() as conn:
        assert store.finish_document(conn, doc_id, text_hash="a" * 64) == 1
    store.delete_many([doc_id])
    with transaction() as conn:
        assert store.finish_document(conn, doc_id, text_hash="b" * 64) == 0


def test_dashboard_url_and_key_come_from_database(ingest_db: None) -> None:
    """向量页控制台按钮：URL 由 vector.url 拼，key 从数据库解密读出。"""
    from app.core.config import store as params_store
    from app.services import vector_admin

    params_store.save_values(
        {"vector.url": "http://127.0.0.1:6333", "vector.api_key": "k" * 43}
    )
    assert vector_admin.dashboard_url() == "http://127.0.0.1:6333/dashboard"
    assert vector_admin.dashboard_key() == "k" * 43


def test_semantic_fallback_uses_soft_boundaries_for_long_quotes() -> None:
    """长篇引号对白先在逗号上软切，不必触发昂贵的语义兜底 embedding。"""
    from app.core.chunk.core import Splitter
    from app.core.parse.model import Block

    calls: list[str] = []

    def fake_semantic(text: str, _budget: int) -> list[str]:
        calls.append(text)
        return [text]

    splitter = Splitter(
        parent_size=2000,
        parent_overlap=100,
        child_size=60,
        child_overlap=10,
        count_tokens=lambda text: max(1, len(text) // 2),
        semantic=fake_semantic,
        min_child_size=10,
    )
    dialogue = "“" + "甲乙丙丁，" * 60 + "”"
    groups = splitter.feed([Block(text=dialogue)]) + splitter.flush()
    children = [child for group in groups for child in group.children]
    assert len(children) > 1
    assert calls == [], "引号内软切能切开时不应调用语义兜底"


def test_cancel_notification_has_no_python_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    """取消 toast 不能带 Python handler（Quasar 只接受 JS，函数会 JSON 序列化失败）。"""
    from app.ui.screens.settings import tasks as ui_tasks

    captured: dict[str, object] = {}

    def fake_notify(message: str, **kwargs: object) -> None:
        captured.update(kwargs)
        captured["message"] = message

    monkeypatch.setattr(ui_tasks.tasks_service, "cancel", lambda _job_id: True)
    monkeypatch.setattr(ui_tasks, "notify", fake_notify)
    ui_tasks._cancel(1)

    assert "已请求取消" in str(captured.get("message", ""))
    assert "actions" not in captured, "notify actions 里不能出现 Python 函数"
