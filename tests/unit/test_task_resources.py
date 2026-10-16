"""任务重复调用时的资源清理（重点盯 inbox 暂存文件）。

**为什么单独一条**：导入任务会**反复调用**——用户重试失败的任务、同一份文件再导一次、
崩溃后下次启动接着跑。如果暂存文件只在"成功路径"顺手删，那么每一次失败都会多留一份残留，
inbox 会无声地越积越大（用户看不见，磁盘在涨）。

这里钉住：

1. 成功、失败、抛异常**三条路径**都要清；
2. 连跑多次不累积；
3. **只清 inbox 内的文件**——payload 的 path 不一定是我们的暂存文件，
   清理动作误删用户自己的文件是不可接受的。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app import tasks as tasks_app
from app.core import paths
from app.core.jobs import api, registry
from app.core.jobs.runner import Runner
from app.services import knowledge
from app.tasks.import_doc import discard_staged

SAMPLE = "# 合同\n\n第一条 乙方应在 30 日内交付设备。\n\n第二条 违约金按日万分之五计算。\n"

#: 老式 .xls：解析器会明确判失败（内容问题，不可重试）——正好用来测"失败也要清"。
UNSUPPORTED = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@pytest.fixture
def library_db(fake_root: Path) -> Iterator[None]:
    """建库 + 干净的注册表 + 自动发现的 kind。"""
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


def _inbox_files() -> list[Path]:
    return [path for path in paths.inbox_dir().glob("*") if path.is_file()]


def _make_inbox() -> Path:
    """建出 inbox 目录（``ensure_dirs`` 按设计**不预建**保留目录，测试自己补）。"""
    folder = paths.inbox_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---------------------------------------------------------------------------
# 清理动作本身
# ---------------------------------------------------------------------------


def test_discard_staged_removes_file_inside_inbox(fake_root: Path) -> None:
    """inbox 里的暂存文件会被删掉。"""
    paths.ensure_dirs()
    staged = _make_inbox() / "abc_合同.md"
    staged.write_text("x", encoding="utf-8")

    discard_staged(staged)
    assert not staged.exists()


def test_discard_staged_is_safe_for_missing_file(fake_root: Path) -> None:
    """文件已经不在也不报错（重复调用/被别处清理过是正常的）。"""
    paths.ensure_dirs()
    _make_inbox()
    discard_staged(paths.inbox_dir() / "不存在.md")  # 不该抛


def test_discard_staged_never_deletes_outside_inbox(fake_root: Path) -> None:
    """**关键安全线**：inbox 之外的文件一律不删（payload 可能指向用户自己的文件）。"""
    paths.ensure_dirs()
    _make_inbox()
    outsider = fake_root / "用户的原件.md"
    outsider.write_text("这是用户自己的文件", encoding="utf-8")

    discard_staged(outsider)

    assert outsider.is_file(), "清理动作误删了 inbox 之外的文件"


# ---------------------------------------------------------------------------
# 端到端：三条路径都不留残留
# ---------------------------------------------------------------------------


def test_successful_import_clears_inbox(library_db: None) -> None:
    """成功路径：跑完 inbox 干净。"""
    payload = knowledge.stage_upload("合同.md", SAMPLE.encode())
    tasks_app.enqueue_import(payload, dedupe_key=knowledge.upload_dedupe_key(SAMPLE.encode()))
    assert _inbox_files(), "上传后应当有一份暂存文件"

    Runner(inline=True).drain()
    assert _inbox_files() == [], "成功跑完要清干净"


def test_failed_import_still_clears_inbox(library_db: None) -> None:
    """**失败路径也要清**：解析失败的任务重试多次也不会把 inbox 撑大。"""
    payload = knowledge.stage_upload("老表格.xls", UNSUPPORTED)
    tasks_app.enqueue_import(payload, dedupe_key=knowledge.upload_dedupe_key(UNSUPPORTED))

    Runner(inline=True).drain()

    assert _inbox_files() == [], "失败了也要清暂存文件（否则每次重试都留一份残留）"


def test_repeated_runs_do_not_accumulate(library_db: None) -> None:
    """连续导入多份（含失败）之后，inbox 一份残留都没有。"""
    for index in range(3):
        data = f"# 资料{index}\n\n内容 {index}。\n".encode()
        payload = knowledge.stage_upload(f"资料{index}.md", data)
        tasks_app.enqueue_import(payload, dedupe_key=knowledge.upload_dedupe_key(data))
    bad = knowledge.stage_upload("坏文件.xls", UNSUPPORTED)
    tasks_app.enqueue_import(bad, dedupe_key=knowledge.upload_dedupe_key(UNSUPPORTED))

    Runner(inline=True).drain()

    assert _inbox_files() == [], "多次运行后 inbox 必须干净"


def test_retrying_a_failed_import_stays_clean(library_db: None) -> None:
    """失败 → 重试 → 仍然没有残留（重试路径同样会被反复调用）。"""
    payload = knowledge.stage_upload("老表格.xls", UNSUPPORTED)
    job_id = tasks_app.enqueue_import(payload, dedupe_key=knowledge.upload_dedupe_key(UNSUPPORTED))

    Runner(inline=True).drain()
    assert _inbox_files() == []

    # 重试（内容问题本来不可重试，这里直接调 api.retry 模拟"用户又点了一次重试"）
    api.retry(job_id)
    Runner(inline=True).drain()

    assert _inbox_files() == [], "重试之后同样不该有残留"
