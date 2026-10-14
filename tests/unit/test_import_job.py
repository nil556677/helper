"""导入任务：上传 → inbox → 队列 → 入库（技术方案 §13 的第一条真实链路）。

钉住四件事：

1. **参数只放引用**：任务表里存路径，不存文件字节（落盘的硬要求）；
2. **幂等**：同一份内容重跑不会重复入库（三级判重兜住 at-least-once）；
3. **解析失败不重试**：内容的问题重试也一样，重试只是让用户多等；
4. **inbox 会清理**：成功失败都不留暂存文件（否则越积越多）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app import tasks as tasks_app
from app.services import knowledge
from app.core.jobs import api, registry
from app.core.jobs.model import FAILED, PENDING, RUNNING
from app.core.jobs.runner import Runner

SAMPLE = "# 设备采购合同\n\n第一条 乙方应在 30 日内交付设备。\n\n第二条 违约金按日万分之五计算。\n"


@pytest.fixture
def library_db(fake_root: Path) -> Iterator[None]:
    """建好 schema v2 的空库 + 干净的注册表 + 已登记的 kind。"""
    from app.core import paths
    from app.core.db import schema
    from app.core.db.connection import close_thread_connection

    paths.ensure_dirs()
    schema.init_db()
    registry.clear()
    tasks_app.register_kinds()
    try:
        yield
    finally:
        registry.clear()
        close_thread_connection()


def _enqueue(name: str, data: bytes) -> int:
    """走界面的真实路径：先落 inbox，再排队。"""
    payload = knowledge.stage_upload(name, data)
    return tasks_app.enqueue_import(payload, dedupe_key=knowledge.upload_dedupe_key(data))


def test_upload_goes_through_queue(library_db: None) -> None:
    """上传先落 inbox、任务只带路径；跑完资料就绪、inbox 被清理。"""
    from app.core import paths

    job_id = _enqueue("设备采购合同.md", SAMPLE.encode())
    job = api.get(job_id)
    assert job is not None and job.kind == "import"
    assert job.payload["path"].endswith(".md")
    assert Path(job.payload["path"]).is_file(), "上传的字节要先落盘（任务表里只放引用）"
    assert list(paths.inbox_dir().glob("*")), "inbox 里应该有一份暂存文件"

    assert Runner(inline=True).drain() == 1

    jobs = api.list_jobs()
    assert jobs[0].status != PENDING
    items = knowledge.documents()
    assert [item["name"] for item in items] == ["设备采购合同.md"]
    assert items[0]["chunks"] >= 2
    assert not list(paths.inbox_dir().glob("*")), "跑完要把 inbox 清干净"


def test_same_content_enqueues_only_once(library_db: None) -> None:
    """同一份内容连点两次：第二条直接复用已有任务（dedupe），不会导两遍。"""
    first = _enqueue("合同.md", SAMPLE.encode())
    second = _enqueue("合同副本.md", SAMPLE.encode())
    assert first == second
    assert len(api.list_jobs()) == 1


def test_parse_failure_does_not_retry(library_db: None) -> None:
    """解析失败（老式 .xls）一次就判失败，并把原因留给人看。"""
    job_id = _enqueue("老表格.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    Runner(inline=True).drain()

    job = api.get(job_id)
    assert job is not None
    assert job.status == FAILED
    assert job.fails == 1, "内容问题不该耗掉重试机会"
    assert "另存为" in str(job.error)

    items = knowledge.documents()
    assert items and items[0]["status"] == "failed"
    assert "另存为" in items[0]["detail"]


def test_rebuild_goes_through_queue(library_db: None) -> None:
    """重建也走队列（以前在界面回调里同步跑：大 PDF 会卡住界面）。

    顺带钉住幂等：重建是"先删片段再写新的"，片段数**不该翻倍**。
    """
    from app.core.db.connection import get_connection

    _enqueue("合同.md", SAMPLE.encode())
    Runner(inline=True).drain()
    doc_id = knowledge.documents()[0]["id"]
    before = int(get_connection().execute("SELECT count(*) AS n FROM chunks").fetchone()["n"])

    job_id = tasks_app.enqueue_rebuild(doc_id)
    assert tasks_app.enqueue_rebuild(doc_id) == job_id, "同一份资料不重复排重建"
    assert Runner(inline=True).drain() == 1

    job = api.get(job_id)
    assert job is not None and job.status == "done", job
    assert "重建完成" in job.note
    after = int(get_connection().execute("SELECT count(*) AS n FROM chunks").fetchone()["n"])
    assert after == before, "重建不该让片段翻倍"


def test_rebuild_of_missing_document_fails_fast(library_db: None) -> None:
    """副本不在了就立刻失败（内容问题，重试也一样）。"""
    job_id = tasks_app.enqueue_rebuild(999)
    Runner(inline=True).drain()

    job = api.get(job_id)
    assert job is not None and job.status == FAILED
    assert job.fails == 1, "内容问题不该耗掉重试机会"


def test_enqueue_and_retry_wake_the_runner(library_db: None) -> None:
    """投递与重试都要立刻叫醒调度器。

    为什么值得单独钉：调度器空闲时是"睡到下一条该跑的时刻"（最长 5 秒），不叫醒它，
    用户点了按钮最多要等 5 秒才看到动静。以前只有 `enqueue_import` 记得唤醒，
    别处直接调 `api.enqueue` / `api.retry` 就会漏——现在收进 `app.core.jobs.api` 里了。
    """
    from app.core.jobs.runner import set_runner

    class FakeRunner:
        """只关心一件事：有没有被叫醒（不真跑任务，用例才确定）。"""

        inline = False

        def __init__(self) -> None:
            self.wakes = 0

        def wake(self) -> None:
            self.wakes += 1

    fake = FakeRunner()
    set_runner(fake)  # type: ignore[arg-type]
    try:
        _enqueue("合同.md", SAMPLE.encode())
        assert fake.wakes == 1, "投递即唤醒"

        bad = _enqueue("老表格.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        Runner(inline=True).drain()
        assert api.get(bad).status == FAILED

        before = fake.wakes
        assert api.retry(bad) is True
        assert fake.wakes == before + 1, "重试也要唤醒"
    finally:
        set_runner(None)


def test_second_upload_of_same_content_is_skipped(library_db: None) -> None:
    """同一篇内容再导一次（字节不同所以能排队）→ 任务成功，但资料不重复。"""
    _enqueue("合同.md", SAMPLE.encode())
    Runner(inline=True).drain()
    assert len(knowledge.documents()) == 1

    # 改了排版（字节不同 → dedupe 放过），正文其实一样 → 入库时被二级判重跳过
    retyped = SAMPLE.replace("\n\n", "\n \n").encode()
    job_id = _enqueue("合同-v2.md", retyped)
    assert Runner(inline=True).drain() == 1

    job = api.get(job_id)
    assert job is not None and job.status == "done"
    assert "已经在资料库里" in job.note
    assert len(knowledge.documents()) == 1, "同一篇内容不该重复入库"
