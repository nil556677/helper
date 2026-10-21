"""加固回归：point id 单射、任务归属令牌、租约回收、取消 TOCTOU、凭证加密、
废弃表清理、重新向量化入口、导入暂存文件保留策略。

这些都是评审里确认过"会真出事"的点，必须由测试钉住（否则下次重构又会悄悄退回去）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.jobs.model import JobError

# ---------------------------------------------------------------------------
# 向量 point id：确定性 + 单射 + 越界保护
# ---------------------------------------------------------------------------


def test_point_id_is_injective() -> None:
    """三段编码必须互不进位：``(0,100,0)`` 与 ``(1,0,0)`` 曾是同一个 id（静默覆盖）。"""
    from app.core.vector.qdrant_store import point_id

    assert point_id(0, 100, 0) != point_id(1, 0, 0)
    assert point_id(2, 100, 0) != point_id(3, 0, 0)
    assert point_id(1, 99, 99_999) != point_id(2, 0, 0)


@pytest.mark.parametrize(
    ("doc_id", "version", "seq"),
    [(0, 100, 0), (1, 0, 0), (12, 3, 7), (999_999, 99, 99_999)],
)
def test_point_id_roundtrip(doc_id: int, version: int, seq: int) -> None:
    from app.core.vector.qdrant_store import parse_point_id, point_id

    assert parse_point_id(point_id(doc_id, version, seq)) == (doc_id, version, seq)


def test_point_id_rejects_overflow() -> None:
    from app.core.vector.qdrant_store import point_id

    with pytest.raises(ValueError):
        point_id(10**7, 0, 0)  # 超出 64 位 id 空间
    with pytest.raises(ValueError):
        point_id(1, 1, 100_000)  # seq 越界


# ---------------------------------------------------------------------------
# 任务：归属令牌 / 回收 / 取消
# ---------------------------------------------------------------------------


def _enqueue_and_claim(kind: str) -> int:
    from app.core.jobs import store

    job_id, _ = store.enqueue(kind, {}, dedupe_key=None)
    claimed = store.claim_next((kind,), lease_s=60.0, worker="cpu@owner")
    assert claimed is not None and claimed.id == job_id
    return job_id


def test_stale_worker_cannot_finish_new_round(ingest_db: None) -> None:
    """被回收后旧线程的收尾必须被忽略，不能把新一轮改成 done。"""
    from app.core.jobs import store

    job_id = _enqueue_and_claim("probe")
    store.finish(job_id, note="陈旧写入", worker="cpu@stale")

    job = store.get_job(job_id)
    assert job is not None and job.status == "running", "陈旧 worker 不该改状态"

    store.finish(job_id, note="正常完成", worker="cpu@owner")
    done = store.get_job(job_id)
    assert done is not None and done.status == "done" and done.note == "正常完成"


def test_reclaim_skips_alive_jobs(ingest_db: None) -> None:
    """线程还活着的任务即使租约过期也不回收（否则双跑）。"""
    from app.core.db.connection import transaction
    from app.core.jobs import store

    job_id = _enqueue_and_claim("probe")
    with transaction() as conn:
        conn.execute("UPDATE jobs SET lease_until = 1 WHERE id = ?", (job_id,))

    alive = store.reclaim_expired(now=10**12, alive=(job_id,))
    assert alive["recovered"] == 0
    assert store.get_job(job_id).status == "running"

    dead = store.reclaim_expired(now=10**12, alive=())
    assert dead["recovered"] == 1
    assert store.get_job(job_id).status == "pending"


def test_cancel_does_not_flip_running_to_cancelled(ingest_db: None) -> None:
    """取消 running 任务只置标志，不能直接把状态改成 cancelled（worker 还在跑）。"""
    from app.core.jobs import store

    job_id = _enqueue_and_claim("probe")
    assert store.request_cancel(job_id) is True

    job = store.get_job(job_id)
    assert job is not None
    assert job.cancel_requested is True
    assert job.status == "running"


def test_cancel_pending_is_immediate(ingest_db: None) -> None:
    from app.core.jobs import store

    job_id, _ = store.enqueue("probe", {}, dedupe_key=None)
    assert store.request_cancel(job_id) is True
    job = store.get_job(job_id)
    assert job is not None and job.status == "cancelled"


def test_recover_cancels_job_that_was_cancelling(ingest_db: None) -> None:
    """取消请求已发出后崩溃：恢复时应直接判 cancelled，**不能**放回队列重跑。"""
    from app.core.db.connection import transaction
    from app.core.jobs import store

    job_id, _ = store.enqueue("probe", {}, dedupe_key=None)
    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', cancel_requested = 1, lease_until = NULL"
            " WHERE id = ?",
            (job_id,),
        )
    store.recover_on_start(now=10**12)
    job = store.get_job(job_id)
    assert job is not None
    assert job.status == "cancelled", "用户已经取消的任务不该被重新跑一遍"


def test_recover_requeues_plain_interrupted_job(ingest_db: None) -> None:
    """普通中断（没请求取消）：放回 pending，并保持 cancel_requested=False。"""
    from app.core.db.connection import transaction
    from app.core.jobs import store

    job_id, _ = store.enqueue("probe", {}, dedupe_key=None)
    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', cancel_requested = 0, lease_until = NULL"
            " WHERE id = ?",
            (job_id,),
        )
    store.recover_on_start(now=10**12)
    job = store.get_job(job_id)
    assert job is not None
    assert job.status == "pending"
    assert job.cancel_requested is False


# ---------------------------------------------------------------------------
# 凭证参数：落库加密、读回解密
# ---------------------------------------------------------------------------


def test_secret_param_is_encrypted_at_rest(ingest_db: None) -> None:
    from app.core.config import store as cfg
    from app.core.db.connection import get_connection

    cfg.save_values({"vector.api_key": "sk-super-secret-1234"})
    raw = get_connection().execute(
        "SELECT value_json FROM settings WHERE key = 'vector.api_key'"
    ).fetchone()["value_json"]

    assert "sk-super-secret-1234" not in raw, "明文凭证不该出现在 settings 里"
    assert raw.startswith('"enc:'), raw[:20]
    assert cfg.load()["vector.api_key"] == "sk-super-secret-1234"


# ---------------------------------------------------------------------------
# schema：废弃表不得被 SCHEMA_SQL 每次启动重建
# ---------------------------------------------------------------------------


def test_deprecated_tables_stay_dropped_after_second_init(ingest_db: None) -> None:
    from app.core.db import schema
    from app.core.db.connection import get_connection

    # 第二次 init 会先跑 SCHEMA_SQL（把 chunks 建回来），收尾清理必须再删掉
    schema.init_db()
    names = schema.table_names(get_connection())
    for gone in ("chunks", "chunks_fts", "codes", "codes_fts", "matrices", "vector_keys"):
        assert gone not in names, f"{gone} 被 SCHEMA_SQL 重建回来了"


# ---------------------------------------------------------------------------
# 重新向量化：走 rebuild（ingest 的 path 必填，旧实现必崩）
# ---------------------------------------------------------------------------


def test_revectorize_enqueues_rebuild(rag_db: None, monkeypatch) -> None:
    from app.services.knowledge import lifecycle

    calls: list[int] = []

    def fake_enqueue(doc_id: int) -> int:
        calls.append(int(doc_id))
        return 100 + int(doc_id)

    monkeypatch.setattr("app.services.knowledge.queue.enqueue_rebuild", fake_enqueue)
    count = lifecycle.revectorize([3, 3, 4])
    assert count == 2
    assert sorted(calls) == [3, 4]


# ---------------------------------------------------------------------------
# 导入任务：可重试失败/中断要留暂存文件，内容问题才删
# ---------------------------------------------------------------------------


class _Ctx:
    cursor = None

    def report(self, *_a: object, **_k: object) -> None: ...

    def set_cursor(self, value: str) -> None:
        self.cursor = value


def _staged() -> Path:
    """在**真实的 inbox** 里造一个暂存文件（discard_staged 只删 inbox 内的）。"""
    from app.core import paths

    folder = paths.inbox_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "上传的文件.md"
    path.write_text("内容", encoding="utf-8")
    return path


def test_ingest_keeps_staged_file_on_retryable_failure(ingest_db: None, monkeypatch) -> None:
    from app.tasks import ingest

    path = _staged()

    class Result:
        status = "failed"
        reason = "写副本失败"
        error_kind = "io"
        doc_id = None
        notes: tuple[str, ...] = ()

    monkeypatch.setattr(ingest.importer, "import_path", lambda *_a, **_k: Result())

    with pytest.raises(JobError):
        ingest.Ingest().run(_Ctx(), ingest.Ingest.Params(path=str(path), name=path.name))
    assert path.is_file(), "可重试失败必须保留暂存文件，否则重试必 not_found"


def test_ingest_deletes_staged_file_on_content_failure(ingest_db: None, monkeypatch) -> None:
    from app.tasks import ingest

    path = _staged()

    class Result:
        status = "failed"
        reason = "格式不支持"
        error_kind = "unsupported"
        doc_id = None
        notes: tuple[str, ...] = ()

    monkeypatch.setattr(ingest.importer, "import_path", lambda *_a, **_k: Result())

    with pytest.raises(JobError):
        ingest.Ingest().run(_Ctx(), ingest.Ingest.Params(path=str(path), name=path.name))
    assert not path.exists(), "内容问题（不可重试）应该删掉暂存文件"


def test_ingest_deletes_staged_file_on_success(ingest_db: None, monkeypatch) -> None:
    from app.tasks import ingest

    path = _staged()

    class Result:
        status = "imported"
        reason = ""
        error_kind = ""
        doc_id = 1
        chunks = 3
        notes: tuple[str, ...] = ()

    monkeypatch.setattr(ingest.importer, "import_path", lambda *_a, **_k: Result())

    result = ingest.Ingest().run(_Ctx(), ingest.Ingest.Params(path=str(path), name=path.name))
    assert result.note.startswith("切出 3")
    assert not path.exists()


def test_ingest_keeps_staged_file_on_interrupt(ingest_db: None, monkeypatch) -> None:
    """关窗口中断：任务会回队列重跑，暂存文件必须还在。"""
    from app.core.jobs.model import JobInterrupted
    from app.tasks import ingest

    path = _staged()

    def interrupted(*_a: object, **_k: object) -> None:
        raise JobInterrupted("应用退出")

    monkeypatch.setattr(ingest.importer, "import_path", interrupted)

    with pytest.raises(JobInterrupted):
        ingest.Ingest().run(_Ctx(), ingest.Ingest.Params(path=str(path), name=path.name))
    assert path.is_file()


# ---------------------------------------------------------------------------
# 监听地址：非回环默认拒绝，显式放行才允许
# ---------------------------------------------------------------------------


def test_listen_host_rejects_non_loopback(fake_root: Path, monkeypatch) -> None:
    import types

    from app import __main__ as entry
    from app.core.config import access

    fake = types.SimpleNamespace(host="0.0.0.0", log_level="INFO", log_debug=False)
    monkeypatch.setattr(access, "load", lambda: fake)
    monkeypatch.delenv("HELPER_ALLOW_INSECURE_HOST", raising=False)
    with pytest.raises(RuntimeError, match="非回环"):
        entry._listen_host()

    monkeypatch.setenv("HELPER_ALLOW_INSECURE_HOST", "1")
    assert entry._listen_host() == "0.0.0.0"


# ---------------------------------------------------------------------------
# 窗口初始位置：固定居中、不记住上次几何
# ---------------------------------------------------------------------------


class _FakeScreen:
    """模拟 QScreen：可用区域（去掉任务栏后的工作区）。"""

    def __init__(self, w: int, h: int, x: int = 0, y: int = 0) -> None:
        self._w, self._h, self._x, self._y = w, h, x, y

    def availableGeometry(self) -> object:
        class _Rect:
            def __init__(self, w: int, h: int, x: int, y: int) -> None:
                self._w, self._h, self._x, self._y = w, h, x, y

            def width(self) -> int:
                return self._w

            def height(self) -> int:
                return self._h

            def x(self) -> int:
                return self._x

            def y(self) -> int:
                return self._y

        return _Rect(self._w, self._h, self._x, self._y)


class _FakeView:
    """模拟 QWebEngineView 的 resize/move。"""

    def __init__(self) -> None:
        self.size: tuple[int, int] | None = None
        self.pos: tuple[int, int] | None = None

    def resize(self, w: int, h: int) -> None:
        self.size = (w, h)

    def move(self, x: int, y: int) -> None:
        self.pos = (x, y)

    def width(self) -> int:
        return self.size[0] if self.size else 0


def test_place_window_uses_preferred_and_centers() -> None:
    """主屏够大时用期望尺寸，且居中于可用区域。"""
    from app.__main__ import _place_window

    view = _FakeView()
    width, height = _place_window(view, _FakeScreen(1920, 1080), (1440, 900))
    assert (width, height) == (1440, 900)
    # 居中：x = (1920 - 1440)//2 = 240，y = (1080 - 900)//2 = 90
    assert view.pos == (240, 90)
    assert view.size == (1440, 900)


def test_place_window_clamps_on_small_screen() -> None:
    """屏幕不够大时按留白收敛（WINDOW_MARGIN=48），且仍居中。"""
    from app.__main__ import WINDOW_MARGIN, _place_window

    view = _FakeView()
    width, height = _place_window(view, _FakeScreen(1366, 768), (1440, 900))
    assert width <= 1366 - WINDOW_MARGIN * 2
    assert height <= 768 - WINDOW_MARGIN * 2
    assert view.pos == (((1366 - width) // 2), ((768 - height) // 2))
    assert view.size == (width, height)


def test_place_window_handles_missing_screen() -> None:
    """无头/受限会话（primaryScreen 为 None）：退回期望尺寸，不崩。"""
    from app.__main__ import _place_window

    view = _FakeView()
    width, height = _place_window(view, None, (1440, 900))
    assert (width, height) == (1440, 900)
    assert view.size == (1440, 900)
