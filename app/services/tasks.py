"""任务系统的**界面侧**服务（UI-free，可直接被 pytest 调用）。

界面只认这里返回的普通数据；表结构、状态机、执行器都在 `app/app/core/jobs`（§13.2 的接缝）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.core.jobs import api
from app.core.jobs.model import CANCELLED, DONE, FAILED, PENDING, RUNNING
from app.core.jobs.runner import get_runner
from app.core.llm.openai_compat import KIND_LABELS as LLM_KIND_LABELS

#: 界面上"正在跑"那一组的状态
ACTIVE_STATUSES = (RUNNING, PENDING)

#: 任务页的筛选分组：key → 状态集合（None 表示不筛）
STATUS_GROUPS: dict[str, tuple[str, ...] | None] = {
    "active": (RUNNING, PENDING),
    "failed": (FAILED,),
    "finished": (DONE, CANCELLED),
    "all": None,
}

#: 筛选按钮的显示顺序与文案
GROUP_LABELS: tuple[tuple[str, str], ...] = (
    ("active", "进行中"),
    ("failed", "失败"),
    ("finished", "已完成"),
    ("all", "全部"),
)

#: "已结束"那两组按**完成时间**倒序（新的在上）；"进行中"按调度顺序（跑着的在前）
_NEWEST_FIRST_GROUPS = frozenset({"failed", "finished"})

#: 状态 → 中文（详情卡用）
_STATUS_LABELS = {
    PENDING: "排队中",
    RUNNING: "正在跑",
    DONE: "完成",
    FAILED: "失败",
    CANCELLED: "已取消",
}

#: 事件/失败分类 → 中文。**复用 §10.1 的九类**（同一套词汇，别造第二套），
#: 再补任务特有的几种：进程中断、被关窗口、用户取消、类型没了。
KIND_LABELS: dict[str, str] = {
    **LLM_KIND_LABELS,
    "interrupted": "被中断（关窗口）",
    "crash": "进程中断（强杀/断电）",
    "cancelled": "用户取消",
    "unsupported": "类型已不存在",
}

#: 任务类型 → 图标（界面用；不认识的就给个通用图标）
_KIND_ICONS = {
    "import": "upload_file",
    "embed": "scatter_plot",
    "ocr": "image",
    "rebuild": "refresh",
    "cleanup": "cleaning_services",
}


def kind_title(kind: str) -> str:
    """任务类型的中文名（注册表里声明的；没注册就退回原样）。"""
    for spec in api.kinds():
        if spec.key == kind:
            return spec.title
    return kind


def kind_icon(kind: str) -> str:
    """任务类型的图标。"""
    return _KIND_ICONS.get(kind, "task_alt")


def describe(job: Any) -> dict[str, Any]:
    """把任务整理成界面直接能画的字段。"""
    name = str(job.payload.get("name") or job.payload.get("source_name") or "")
    if not name and job.payload.get("doc_id"):
        name = f"资料 #{job.payload['doc_id']}"
    return {
        "id": job.id,
        "kind": job.kind,
        "title": kind_title(job.kind),
        "icon": kind_icon(job.kind),
        "name": name,
        "status": job.status,
        "status_label": _STATUS_LABELS.get(job.status, job.status),
        "progress": job.progress,
        "done": job.done,
        "total": job.total,
        "note": job.note,
        "error": job.error or "",
        "error_kind": job.error_kind or "",
        "error_label": KIND_LABELS.get(job.error_kind or "", ""),
        "attempts": job.fails,
        "max_attempts": job.max_attempts,
        "cancelling": job.cancelled_pending,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "queue_position": 0,
    }


def detail(job_id: int) -> dict[str, Any] | None:
    """一个任务的**完整情况**：字段 + 事件时间线（每次失败/中断/取消各一笔）。

    界面上"详情"弹卡用它：用户点开就能看到"失败了几次、每次什么原因、有没有完整报错"，
    而不用去翻日志文件。
    """
    job = api.get(job_id)
    if job is None:
        return None
    data = describe(job)
    data["events"] = [
        {
            "at": event.at,
            "attempt": event.attempt,
            "kind": event.kind,
            "kind_label": KIND_LABELS.get(event.kind, event.kind),
            "message": event.message,
            "detail": event.detail,
        }
        for event in api.events(job_id)
    ]
    return data


def active_count() -> int:
    """活跃任务数（排队 + 在跑）——左栏徽标用它，所以必须便宜（一条走索引的计数）。"""
    return int(api.stats()["active"])


def overview(*, group: str = "active", page: int = 1, size: int = 20) -> dict[str, Any]:
    """任务页一屏所需的数据（**服务端分页**）。

    为什么要分页：队列可能积几百条、历史可能上万条——"全查出来再切"既慢又没人看。

    "正在跑"**单独取、不分页**：它是"现在在干什么"的答案，无论筛选到哪一组都该看见；
    而且它受泳道并发限制（cpu 1 + io 2），永远不会多到需要分页。

    参数：
        group: 筛选分组（:data:`STATUS_GROUPS` 的 key）。
        page: 页码（1 基）。
        size: 每页条数。
    返回：``{'stats', 'running', 'items', 'total', 'page', 'pages', 'group', 'paused',
    'degraded', 'hint'}``。
    """
    from math import ceil

    stats = api.stats()
    running = [describe(job) for job in api.list_jobs(statuses=(RUNNING,), limit=50)]
    statuses = STATUS_GROUPS.get(group, STATUS_GROUPS["active"])
    total = api.count_jobs(statuses=statuses)
    size = max(1, int(size))
    pages = max(1, ceil(total / size))
    current = min(max(1, int(page)), pages)
    jobs = api.list_jobs(
        statuses=statuses,
        limit=size,
        offset=(current - 1) * size,
        newest_first=group in _NEWEST_FIRST_GROUPS,
    )
    items = [describe(job) for job in jobs]
    if group == "active":
        # "进行中"里排队那几条要显示"前面还有几个"，位置按队列顺序算
        for position, item in enumerate(items, start=1):
            if item["status"] == PENDING:
                item["queue_position"] = position
    runner = get_runner()
    return {
        "stats": stats,
        "running": running,
        "items": items,
        "total": total,
        "page": current,
        "pages": pages,
        "size": size,
        "group": group if group in STATUS_GROUPS else "active",
        "paused": api.is_paused(),
        "degraded": bool(runner is not None and runner.lag_degraded),
        "hint": last_error_hint(),
    }


def cancel(job_id: int) -> bool:
    """取消一个任务（排队中的立刻取消；在跑的等它到批次边界）。"""
    return api.cancel(job_id)


def retry(job_id: int) -> bool:
    """重试一个失败/已取消的任务。"""
    return api.retry(job_id)


def retry_all_failed() -> int:
    """把所有失败的重新排队，返回条数。"""
    count = 0
    for job in api.list_jobs(statuses=(FAILED,), limit=200):
        if api.retry(job.id):
            count += 1
    return count


def cancel_many(job_ids: Iterable[int]) -> int:
    """批量取消（排队中的立刻取消，在跑的等批次边界）。返回实际处理的条数。"""
    return sum(1 for job_id in job_ids if api.cancel(int(job_id)))


def retry_many(job_ids: Iterable[int]) -> int:
    """批量重试（只对失败/已取消的有效）。返回实际处理的条数。"""
    return sum(1 for job_id in job_ids if api.retry(int(job_id)))


def delete_many(job_ids: Iterable[int]) -> int:
    """批量删任务记录（连带事件）。

    **只删已结束的**：排队/在跑的是用户还在等的事——误删等于把活弄丢了。
    """
    from app.core.jobs import store

    return store.delete_jobs([int(job_id) for job_id in job_ids])


def clear_finished() -> int:
    """清掉已完成/失败/取消的任务（界面上的「清理已完成」：**立刻**清干净，不套保留策略）。"""
    from app.core.jobs import store

    return store.delete_finished()


def pause() -> None:
    """暂停队列（不再开新活；在跑的跑到批次边界挂起）。"""
    api.pause()


def resume() -> None:
    """恢复队列。"""
    api.resume()


def last_error_hint() -> str:
    """给界面的一句话：队列为什么慢/停了（没有就返回空串）。"""
    runner = get_runner()
    if runner is not None and runner.lag_degraded:
        return "为保持界面流畅，向量化一类吃 CPU 的任务已临时让路"
    if api.is_paused():
        return "队列已暂停：正在跑的会收尾，不再开新活"
    return ""
