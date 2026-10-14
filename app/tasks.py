"""任务系统的装配处（**项目级**：所有功能的任务类型都在这里注册）。

为什么集中在一处：看一眼就知道"这个项目有哪些后台任务"；而 handler 实现留在各自的功能里
（`app/services/<功能>.py`），register 只是把它们登记给引擎。

界面、功能都只通过这里启动/停止执行器；`app/app/core/jobs` 不认识任何具体功能（技术方案 §13）。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from loguru import logger

from app.services import knowledge as knowledge_service
from app.services import maintenance as maintenance_service
from app.core.jobs import api
from app.core.jobs.registry import JobKind
from app.core.jobs.runner import Runner, get_runner, set_runner

#: 事件循环延迟的采样间隔与窗口（窗口 40 × 50ms = 最近 2 秒）
LAG_INTERVAL_S = 0.05
LAG_WINDOW = 40

_lag_samples: deque[float] = deque(maxlen=LAG_WINDOW)
_lag_task: asyncio.Task | None = None


def register_kinds() -> None:
    """登记所有任务类型（幂等：已注册过就跳过，重复注册引擎会报错）。"""
    existing = {spec.key for spec in api.kinds()}
    specs = (
        JobKind(
            key="import",
            title="导入资料",
            lane="cpu",  # 解析/切分是 CPU 活（第三方解析库都是同步的）
            handler=knowledge_service.run_import_job,
            idempotent=True,  # 内部三级判重，重跑安全
            max_attempts=3,
            lease_s=120.0,  # 大 PDF 解析可能几十秒
        ),
        JobKind(
            key="rebuild",
            title="重建索引",
            lane="cpu",  # 同样是解析 + 切分
            handler=knowledge_service.run_rebuild_job,
            idempotent=True,  # 先删片段再写新的，重跑结果一样
            max_attempts=3,
            lease_s=120.0,
        ),
        JobKind(
            key="cleanup",
            title="清理存储",
            lane="cpu",  # 扫目录 + 删文件 + 合并 WAL，都是 IO/CPU 混合的短活
            handler=maintenance_service.run_cleanup_job,
            idempotent=True,  # 删的都是"没人认领"的东西，重跑结果一样
            max_attempts=2,
            lease_s=300.0,  # 目录很大时要扫一会儿
        ),
    )
    for spec in specs:
        if spec.key not in existing:
            api.register(spec)


def lag_ms() -> float:
    """最近 2 秒里事件循环延迟的 p95（毫秒）——调度器据此在界面卡顿时让路。"""
    if not _lag_samples:
        return 0.0
    ordered = sorted(_lag_samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


async def _measure_lag() -> None:
    """每 50ms 量一次"这次醒来比预期晚了多少"。"""
    last = time.perf_counter()
    while True:
        await asyncio.sleep(LAG_INTERVAL_S)
        now = time.perf_counter()
        _lag_samples.append(max(0.0, (now - last - LAG_INTERVAL_S) * 1000.0))
        last = now


def ensure_lag_monitor() -> None:
    """在**事件循环里**启动延迟测量（页面构建时调；重复调只启动一次）。

    为什么要在页面构建时启动：那里才确定拿得到 NiceGUI 服务线程的 asyncio 循环。
    """
    global _lag_task
    if _lag_task is not None and not _lag_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 不在事件循环里（比如某些测试）→ 不启动，延迟读数为 0
        return
    _lag_task = loop.create_task(_measure_lag())


def start() -> Runner:
    """启动任务执行器（应用入口在 `ensure_ready()` 之后调用）。

    返回：进程级执行器（后续 `stop()` 用）。
    """
    register_kinds()
    runner = get_runner()
    if runner is None:
        runner = Runner(lag_provider=lag_ms)
        set_runner(runner)
    runner.start()
    enqueue_cleanup("startup")  # 启动时顺手维护一次（维护类，priority=-10，不抢用户的活）
    logger.info("任务系统就绪：{} 种任务类型", len(api.kinds()))
    return runner


def enqueue_cleanup(reason: str = "manual") -> int:
    """排一条清理任务（孤儿副本 / inbox 残留 / WAL / 历史）。

    **去重**：`dedupe_key="cleanup"` 保证同一时刻只有一条在队列里——
    启动一次就排一条的话，用户频繁开关窗口会把队列塞满维护任务。
    """
    from app.core.jobs.model import PRIORITY_MAINTENANCE

    return api.enqueue("cleanup", {"reason": reason}, dedupe_key="cleanup", priority=PRIORITY_MAINTENANCE)


def stop(grace_s: float = 3.0) -> None:
    """优雅退出：让在跑的任务收尾并放回队列（下次打开接着做）。"""
    runner = get_runner()
    if runner is not None:
        runner.stop(grace_s=grace_s)


def enqueue_import(payload: dict[str, str], *, dedupe_key: str, user_waiting: bool = True) -> int:
    """排一条导入任务（用户在等 → 优先级最高，别让它排在后台活后面）。

    唤醒由 `app.core.jobs.api.enqueue` 负责（它认识本进程的调度器），这里不用重复做。
    """
    from app.core.jobs.model import PRIORITY_DEFAULT, PRIORITY_WAITING

    return api.enqueue(
        "import",
        payload,
        dedupe_key=dedupe_key,
        priority=PRIORITY_WAITING if user_waiting else PRIORITY_DEFAULT,
    )


def enqueue_rebuild(doc_id: int) -> int:
    """排一条"重建索引"任务（用副本重新解析与切分）。

    **为什么它必须走队列**：重建要重新解析整份文件（大 PDF 好几秒），以前是在界面回调里
    同步做的——那会卡住界面（架构里最不该出现的"同步重活"）。走队列之后进度在任务页可见，
    关窗口也不丢。
    """
    from app.core.jobs.model import PRIORITY_WAITING

    return api.enqueue(
        "rebuild",
        {"doc_id": int(doc_id)},
        dedupe_key=f"rebuild:doc:{int(doc_id)}",  # 同一份资料不重复排
        priority=PRIORITY_WAITING,
    )


def enqueue_rebuild_many(doc_ids: list[int]) -> int:
    """批量重建（同一份资料排两次会被 dedupe 合成一条）。返回排进去的条数。"""
    return len({enqueue_rebuild(int(doc_id)) for doc_id in doc_ids})
