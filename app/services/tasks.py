"""任务系统的**界面侧**服务（UI-free，可直接被 pytest 调用）。

界面只认这里返回的**类型化视图模型**（§8.2 的接缝）：表结构、状态机、执行器都在
`app/core/jobs`；本模块把 `Job` 这类领域对象整理成界面直接能画的形状。

**为什么返回 dataclass 而不是 dict**：层与层之间用字符串键传数据时，**改一个字段名不会有任何
地方报错**——只能靠测试发现。返回 `frozen dataclass` 之后，界面写 `job.note` 写错当场可见；
也让"这一屏到底需要哪些字段"成为一份可读的契约（见 `docs/分层与类型化改造方案.md`）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
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
#: 再补任务特有的几种：进程中断、被关窗口、用户取消、类型没了、执行结果。
KIND_LABELS: dict[str, str] = {
    **LLM_KIND_LABELS,
    "interrupted": "被中断（关窗口）",
    "crash": "进程中断（强杀/断电）",
    "cancelled": "用户取消",
    "unsupported": "类型已不存在",
    "result": "执行结果",
    # 切分诊断（chunk 层写进 job_events，界面按这些名字显示）
    "chunking": "切分失败",
    "hard_cut_used": "硬切（无标点长段）",
    "atomic_line_over_budget": "表格/代码单行超长",
    "image_skipped": "图片未 OCR",
    "child_over_budget": "片段超预算",
    "min_child_violated": "片段低于下限",
    "heading_too_long": "标题超预算",
    "empty_blocks": "没有可切内容",
    "no_sentence_boundary": "整段无句末标点",
    "heading_without_body": "标题没有正文",
    "embed_budget_exceeded": "切片超嵌入预算",
    "doc_title_multi_h1": "多个一级标题",
    "byline_detected": "识别到作者行",
}

#: 参数值在界面上最多显示多少个字符（长路径/长文本截断，完整值靠 tooltip）
PARAM_MAX_CHARS = 120

#: 任务类型 → 图标（界面用；不认识的就给个通用图标）
_KIND_ICONS = {
    "ingest": "upload_file",
    "ocr": "image",
    "rebuild": "refresh",
    "cleanup": "cleaning_services",
}


# ---------------------------------------------------------------------------
# 视图模型（界面只读它们）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamItem:
    """一项任务参数（界面逐行显示；``full`` 是未截断的原值，挂在 tooltip 上）。"""

    key: str
    value: str
    full: str


@dataclass(frozen=True)
class TaskEventView:
    """事件时间线里的一条（失败/中断/取消/结果的经过）。"""

    at: float
    attempt: int
    kind: str
    kind_label: str
    message: str
    detail: str


@dataclass(frozen=True)
class TaskStats:
    """各状态计数（左栏徽标与页面状态行用）。"""

    pending: int
    running: int
    done: int
    failed: int
    cancelled: int
    active: int


@dataclass(frozen=True)
class Concurrency:
    """当前并发情况（界面显示"并发 x/y"）。"""

    running: int
    limit: int
    degraded: bool
    serialized: bool = False
    """是否被资源锁串行（如 ONNX）：limit 是全局上限，但实际同时只能跑 1 条。"""


@dataclass(frozen=True)
class TaskView:
    """任务页一行所需的**全部**字段。

    ``from_job`` 是「行 → 视图模型」的唯一映射处：新增字段只改这里一处。
    """

    id: int
    kind: str
    title: str
    icon: str
    name: str
    status: str
    status_label: str
    progress: float
    done: int
    total: int
    note: str
    result: str
    params: tuple[ParamItem, ...]
    error: str
    error_kind: str
    error_label: str
    attempts: int
    max_attempts: int
    cancelling: bool
    created_at: float
    started_at: float | None
    finished_at: float | None
    duration_s: float | None
    updated_at: float
    queue_position: int = 0

    @classmethod
    def from_job(cls, job: Any, *, now: float | None = None) -> TaskView:
        """把一个 ``Job`` 变成界面要的形状（**唯一**映射处）。"""
        name = str(job.payload.get("name") or job.payload.get("source_name") or "")
        if not name and job.payload.get("doc_id"):
            name = f"资料 #{job.payload['doc_id']}"
        return cls(
            id=job.id,
            kind=job.kind,
            title=kind_title(job.kind),
            icon=kind_icon(job.kind),
            name=name,
            status=job.status,
            status_label=_STATUS_LABELS.get(job.status, job.status),
            progress=job.progress,
            done=job.done,
            total=job.total,
            note=job.note,
            result=job.note,
            params=_format_params(job.payload),
            error=job.error or "",
            error_kind=job.error_kind or "",
            error_label=KIND_LABELS.get(job.error_kind or "", ""),
            attempts=job.fails,
            max_attempts=job.max_attempts,
            cancelling=job.cancelled_pending,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            duration_s=_duration_s(job, now=now),
            updated_at=job.updated_at,
        )


@dataclass(frozen=True)
class TaskDetail(TaskView):
    """详情弹窗用：一行所需的字段 + 事件时间线。"""

    events: tuple[TaskEventView, ...] = ()


@dataclass(frozen=True)
class TaskOverview:
    """任务页一屏所需的全部数据（服务端分页）。"""

    stats: TaskStats
    running: tuple[TaskView, ...]
    items: tuple[TaskView, ...]
    total: int
    page: int
    pages: int
    size: int
    group: str
    paused: bool
    concurrency: Concurrency
    degraded: bool
    hint: str


# ---------------------------------------------------------------------------
# 内部：领域对象 → 视图模型
# ---------------------------------------------------------------------------


def _duration_s(job: Any, *, now: float | None = None) -> float | None:
    """这次执行花了多久（秒）：开始过才有；还在跑就按"到现在"算。"""
    started = job.started_at
    if not started:
        return None
    ended = job.finished_at or (time.time() if now is None else now)
    return max(0.0, float(ended) - float(started))


def _format_params(payload: dict[str, Any]) -> tuple[ParamItem, ...]:
    """把参数整理成界面能逐行显示的键值对（值统一成短字符串）。"""
    items: list[ParamItem] = []
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        elif value is None:
            text = "—"
        else:
            text = str(value)
        items.append(
            ParamItem(
                key=str(key),
                value=text if len(text) <= PARAM_MAX_CHARS else text[:PARAM_MAX_CHARS] + "…",
                full=text,
            )
        )
    return tuple(items)


def _stats() -> TaskStats:
    """各状态计数（core 给的是 dict，这里转成类型化对象）。"""
    raw = api.stats()
    return TaskStats(
        pending=int(raw.get(PENDING, 0)),
        running=int(raw.get(RUNNING, 0)),
        done=int(raw.get(DONE, 0)),
        failed=int(raw.get(FAILED, 0)),
        cancelled=int(raw.get(CANCELLED, 0)),
        active=int(raw.get("active", 0)),
    )


# ---------------------------------------------------------------------------
# 任务类型元信息
# ---------------------------------------------------------------------------


def kind_title(kind: str) -> str:
    """任务类型的中文名（注册表里声明的；没注册就退回原样）。"""
    for spec in api.kinds():
        if spec.key == kind:
            return spec.title
    return kind


def kind_icon(kind: str) -> str:
    """任务类型的图标。"""
    return _KIND_ICONS.get(kind, "task_alt")


def concurrency() -> Concurrency:
    """当前并发情况（界面显示"并发 x/y"）：在跑几个、上限几个、是不是被让路了。

    **worker 模式**下任务跑在独立 worker 进程，主进程没有 Runner；不能用
    ``get_runner()`` 判断在跑数（会永远显示 0）。改为从数据库查 running 条数，
    并标注"ONNX 串行"——ingest/rebuild 等 kind 声明了 ``resource="onnx"``，
    同名资源同时只允许一条，所以即使全局上限 3 也只会跑 1 条。
    """
    from app.core.jobs.runner import MAX_CONCURRENT_TASKS

    runner = get_runner()
    if runner is None:
        try:
            running = int(api.count_jobs(statuses=("running",)))
        except Exception:
            running = 0
        return Concurrency(
            running=running,
            limit=MAX_CONCURRENT_TASKS,
            degraded=False,
            serialized=True,
        )
    return Concurrency(
        running=runner.running_count,
        limit=runner.max_concurrent,
        degraded=bool(runner.lag_degraded),
    )


# ---------------------------------------------------------------------------
# 列表与详情
# ---------------------------------------------------------------------------


def detail(job_id: int) -> TaskDetail | None:
    """一个任务的**完整情况**：字段 + 事件时间线（每次失败/中断/取消/结果各一笔）。

    界面上"详情"弹卡用它：用户点开就能看到"带了什么参数、跑了多久、结果是什么、
    失败了几次、每次什么原因"，而不用去翻日志文件。
    """
    job = api.get(job_id)
    if job is None:
        return None
    view = TaskView.from_job(job)
    events = tuple(
        TaskEventView(
            at=event.at,
            attempt=event.attempt,
            kind=event.kind,
            kind_label=KIND_LABELS.get(event.kind, event.kind),
            message=event.message,
            detail=event.detail,
        )
        for event in api.events(job_id)
    )
    return TaskDetail(**vars(view), events=events)


def active_count() -> int:
    """活跃任务数（排队 + 在跑）——左栏徽标用它，所以必须便宜（一条走索引的计数）。"""
    return int(api.stats()["active"])


def overview(*, group: str = "active", page: int = 1, size: int = 20) -> TaskOverview:
    """任务页一屏所需的数据（**服务端分页**）。

    为什么要分页：队列可能积几百条、历史可能上万条——"全查出来再切"既慢又没人看。

    "正在跑"**单独取、不分页**：它是"现在在干什么"的答案，无论筛选到哪一组都该看见；
    而且它受全局并发上限（默认 3）限制，永远不会多到需要分页。

    参数：
        group: 筛选分组（:data:`STATUS_GROUPS` 的 key）。
        page: 页码（1 基）。
        size: 每页条数。
    """
    from math import ceil

    stats = _stats()
    running = tuple(TaskView.from_job(job) for job in api.list_jobs(statuses=(RUNNING,), limit=50))
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
    items = [TaskView.from_job(job) for job in jobs]
    if group == "active":
        # "进行中"里排队那几条要显示"前面还有几个"，位置按队列顺序算
        items = [
            replace(item, queue_position=position) if item.status == PENDING else item
            for position, item in enumerate(items, start=1)
        ]
    runner = get_runner()
    return TaskOverview(
        stats=stats,
        running=running,
        items=tuple(items),
        total=total,
        page=current,
        pages=pages,
        size=size,
        group=group if group in STATUS_GROUPS else "active",
        paused=api.is_paused(),
        concurrency=concurrency(),
        degraded=bool(runner is not None and runner.lag_degraded),
        hint=last_error_hint(),
    )


# ---------------------------------------------------------------------------
# 动作（界面按钮直接调）
# ---------------------------------------------------------------------------


def _cleanup_unstarted_import(job: object) -> None:
    """取消一个**从未开始**的导入任务时，清掉它的占位行与暂存文件。

    为什么需要：上传时就落了 ``documents`` 占位行（``stored_path=''``），副本要等
    任务真正开始才写。任务排队时被取消 → 留下一个"没有副本"的占位行：重建会报
    "找不到副本"，同文件重新上传还会被当成重复。这里把它清掉，使"取消"和"删除资料"
    两种操作先后顺序都成立。

    参数：
        job: 取消前的任务对象（需要 ``payload``）。
    """
    from pathlib import Path

    from loguru import logger

    from app.core import paths
    from app.rag import kb_store

    payload = dict(getattr(job, "payload", None) or {})
    doc_id = payload.get("doc_id")
    raw = str(payload.get("path") or "")

    # 先看还有没有别的活任务引用同一个 doc / 同一个 inbox 文件：
    # 有的话不能删，否则会把正在跑的导入的输入/占位行一起清掉。
    active_docs: set[int] = set()
    active_paths: set[str] = set()
    current_id = getattr(job, "id", None)
    try:
        for other in api.list_jobs(statuses=("pending", "running"), limit=500):
            if current_id is not None and getattr(other, "id", None) == current_id:
                continue
            other_payload = getattr(other, "payload", None) or {}
            if not isinstance(other_payload, dict):
                continue
            try:
                if other_payload.get("doc_id") is not None:
                    active_docs.add(int(other_payload["doc_id"]))
            except (TypeError, ValueError):
                pass
            if other_payload.get("path"):
                active_paths.add(str(other_payload["path"]))
    except Exception:
        pass

    if doc_id is not None:
        try:
            row = kb_store.get_document(int(doc_id))
        except Exception as exc:
            logger.warning("取消导入时读占位行失败（doc {}）：{}", doc_id, exc)
            row = None
        if row is not None:
            stored = str(row["stored_path"] or "")
            status = str(row["status"] or "")
            if status == "pending" and not stored and int(doc_id) not in active_docs:
                kb_store.delete_document(int(doc_id))
    # 暂存文件只删 inbox 里的那一个（防止 payload 被手工改成任意路径）
    if raw and raw not in active_paths:
        candidate = Path(raw)
        try:
            inbox = paths.inbox_dir().resolve()
            if candidate.is_file() and candidate.resolve().parent == inbox:
                candidate.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("删除暂存文件失败（{}）：{}", candidate, exc)


def cancel(job_id: int) -> bool:
    """取消一个任务（排队中的立刻取消；在跑的等它到批次边界）。

    对**从未开始的导入**：取消成功后顺手清掉占位行与暂存文件（否则会留下
    "没有副本、重建必失败、重新上传又被判重复"的孤儿行）。
    """
    job = api.get(job_id)
    was_unstarted_import = bool(
        job is not None
        and job.kind == "ingest"
        and job.status == "pending"
        and job.started_at is None
    )
    ok = api.cancel(job_id)
    if ok and was_unstarted_import and job is not None:
        _cleanup_unstarted_import(job)
    return ok


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
    return sum(1 for job_id in job_ids if cancel(int(job_id)))


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
