"""任务系统的对外接口（技术方案 §13.2）——**界面与功能只调这里**。

本模块只管"排队与状态"；执行（调度线程、泳道、优雅退出）由 `runner.py` 提供（M2）。
这样界面侧永远不会误碰 SQL，功能侧也不需要知道表结构。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.core.jobs import registry, store
from app.core.jobs.model import PRIORITY_DEFAULT, Job, JobEvent
from app.core.jobs.registry import JobKind


def _wake_runner() -> None:
    """叫醒本进程的调度器（投递/重试之后立刻开跑）。

    **为什么放在这一层**：调度器在空闲时会"睡到下一条该跑的时刻"（最长 5 秒），
    如果投递时不叫醒它，用户点了按钮最多要等 5 秒才看到动静。以前只有
    `app/tasks.enqueue_import` 记得唤醒，别处直接调 `enqueue` 就会漏——现在收进这里，
    谁投递都不会忘。跨进程投递没有这个便利（唤醒事件在别的进程里），靠轮询兜底。
    """
    from app.core.jobs.runner import get_runner

    runner = get_runner()
    if runner is not None and not runner.inline:
        runner.wake()


def register(spec: JobKind) -> None:
    """注册一种任务（功能在启动时调用一次）。

    异常：
        ValueError: 声明不合法，或同一 key 注册了两次（早报错，别等运行期）。
    """
    registry.register(spec)
    logger.debug("注册任务类型：{}（泳道 {}）", spec.key, spec.lane)


def register_all(specs: list[JobKind] | tuple[JobKind, ...]) -> None:
    """批量注册（**原子**：全部合规才登记）。

    启动时的自动加载走这里——宁可一条都不注册、直接报错，
    也不要留下"只注册了一半"的注册表（那会让一半任务凭空消失）。
    """
    batch = list(specs)
    registry.register_all(batch)
    for spec in batch:
        logger.debug("注册任务类型：{}（泳道 {}）", spec.key, spec.lane)


def kinds() -> tuple[JobKind, ...]:
    """所有已注册的任务类型（任务页显示标题用）。"""
    return registry.registered()


def enqueue(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    priority: int = PRIORITY_DEFAULT,
    delay_s: float = 0.0,
) -> int:
    """把一件事排进队列，返回任务 id。

    - **同 dedupe_key 已在队列里（排队或在跑）时，返回那条已存在的任务**，不会排两次；
    - `max_attempts` 在这一刻从 kind 快照进行，之后改 kind 的默认值不影响已排的任务；
    - **参数在这里统一校验/规范化**（任务类声明的 `normalize`）：非法参数在入队那一刻
      就被拒（抛 ``ValueError``），而不是等任务跑起来才失败——排队半天再报"参数错"最气人。

    异常：
        KeyError: kind 没注册过（消息是人话）。
        ValueError: 参数不合该任务的形状（消息里说明哪个字段错）。
    """
    spec = registry.get(kind)
    normalized = spec.normalize(dict(payload or {})) if spec.normalize is not None else dict(payload or {})
    job_id, created = store.enqueue(
        kind,
        normalized,
        dedupe_key=dedupe_key,
        priority=priority,
        delay_s=delay_s,
        max_attempts=spec.max_attempts,
        resource=spec.resource,
        payload_version=spec.payload_version,
    )
    if created:
        logger.info("任务入队：{} #{}（优先级 {}）", spec.title, job_id, priority)
        _wake_runner()
    else:
        logger.debug("任务已在队列里，复用：#{}（{}）", job_id, kind)
    return job_id


def cancel(job_id: int) -> bool:
    """请求取消：``pending`` 立刻取消；``running`` 在批次边界响应。"""
    ok = store.request_cancel(job_id)
    if ok:
        logger.info("已请求取消任务 #{}", job_id)
    return ok


def retry(job_id: int) -> bool:
    """手动重试一个失败/已取消的任务（``fails`` 归零）。"""
    ok = store.retry_row(job_id)
    if ok:
        logger.info("任务 #{} 重新排队", job_id)
        _wake_runner()
    return ok


def get(job_id: int) -> Job | None:
    """按 id 取任务。"""
    return store.get_job(job_id)


def list_jobs(
    *,
    statuses: tuple[str, ...] | None = None,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
    newest_first: bool = False,
) -> list[Job]:
    """列任务（正在跑 → 排队 → 已结束）；``offset`` 支持分页，``newest_first`` 按完成时间倒序。"""
    return store.list_jobs(
        statuses=statuses, kind=kind, limit=limit, offset=offset, newest_first=newest_first
    )


def count_jobs(*, statuses: tuple[str, ...] | None = None, kind: str | None = None) -> int:
    """数任务（分页的总数）。"""
    return store.count_jobs(statuses=statuses, kind=kind)


def events(job_id: int, *, limit: int = 50) -> list[JobEvent]:
    """某个任务的事件时间线（失败 / 中断 / 取消 / 崩溃，按发生顺序）。"""
    return store.events(job_id, limit=limit)


def stats() -> dict[str, int]:
    """各状态计数（`active` 是排队 + 在跑，徽标显示它）。"""
    return store.stats()


def pause() -> None:
    """暂停队列：不再认领新任务；正在跑的会跑完当前批次后在边界挂起（**持久**）。"""
    store.set_paused(True)
    logger.info("任务队列已暂停")


def resume() -> None:
    """恢复队列。"""
    store.set_paused(False)
    logger.info("任务队列已恢复")


def is_paused() -> bool:
    """队列是否暂停（持久化在 settings 表里，重启仍生效）。"""
    return store.get_paused()
