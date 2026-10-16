"""统一弹窗组件（components/dialog.py）经真实界面触发的行为测试。

不注册独立页面——user 装置只加载 `nicegui_main.py` 里的 `/` 路由，测试里 `@ui.page`
注册的新路由不会被挂载（会 404）。所以走真实设置页触发弹窗。

这里钉一条：任务详情弹窗（``custom``）经统一壳后仍能正常开、内容完整、能关——
这印证"统一改装没把复杂弹窗做坏"。
（批量删除 → 统一 confirm 的出/删闭环已由 test_shell.py 的
 `test_bulk_delete_documents_from_the_page` 覆盖。）
"""

from __future__ import annotations

from typing import Any

from nicegui import ui


async def _open_tasks(user: Any) -> None:
    """进到 设置 → 任务 页。"""
    await user.open('/')
    user.find(marker='pin-settings').click()
    user.find(marker='section-tasks').click()


async def test_failed_job_detail_dialog_renders(user: Any) -> None:
    """任务详情弹窗（custom）：失败任务点开能看时间线 + 关闭按钮——统一壳没把它做坏。"""
    from app import tasks as tasks_app
    from app.core.jobs import api, store
    from app.core.jobs import registry as jobs_registry

    tasks_app.register_kinds()
    try:
        job_id = api.enqueue("import", {"path": "（测试）", "name": "详情单.md"}, dedupe_key="d:1")
        store.claim_next(("import",), lease_s=60.0, worker="cpu@test")
        store.fail(job_id, error="解析失败", retryable=False, kind="unsupported", detail="第 1 行语法错")

        await _open_tasks(user)
        user.find(marker='task-group-failed').click()
        user.find(marker=f'task-details-{job_id}').click()

        await user.should_see('详情单.md')
        await user.should_see('第 1 次')
        await user.should_see('完整信息（报错原文）')

        # 统一壳应该还留着「关闭」按钮（custom 的调用方负责放）
        buttons = [str(getattr(b, 'text', '')) for b in user.find(kind=ui.button).elements]
        assert '关闭' in buttons, buttons
    finally:
        jobs_registry.clear()
