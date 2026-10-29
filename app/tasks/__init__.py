"""任务系统的装配处（**项目级**：所有任务都在 ``app/tasks/`` 下，启动时自动发现注册）。

**怎么写一个任务**：在 ``app/tasks/`` 下新建一个文件，写**一个** :class:`~app.core.jobs.Task`
子类：声明 ``title``（界面显示名），**标识由类名派生**（``ImportTask`` → ``import``），
实现 ``run(ctx, params)`` 并返回 :class:`~app.core.jobs.TaskResult`；参数用嵌套的 ``Params``
（pydantic 模型）声明。**新增任务 = 新建一个文件**，装配处一行都不用改。

**三条不变量**（都由 ``tests/unit/test_task_discovery.py`` 钉住）：

1. **可重复调用**——发现只读模块里的任务类，不依赖 import 副作用；``registry.clear()``
   之后再调 ``register_kinds()`` 照样能注册回来（测试要重建注册表，这条是硬要求）。
2. **失败即崩**——某个任务模块导入失败、或类不合规，**必须抛**，绝不静默跳过。
   静默少注册一个 kind，``runner`` 会把用户排队中的该类型任务判成"类型已不存在"。
3. **先校验后登记**——全部合规才动注册表；任一条不合规就整体不登记，不留半成品。

界面、功能都只通过这里启动/停止执行器；``app/core/jobs`` 不认识任何具体功能（技术方案 §13）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable

from loguru import logger

from app.core.jobs import api
from app.core.jobs.runner import Runner, get_runner
from app.tasks._discovery import discovered_kinds

#: 事件循环延迟的采样间隔与窗口（窗口 40 × 50ms = 最近 2 秒）
LAG_INTERVAL_S = 0.05
LAG_WINDOW = 40

_lag_samples: deque[float] = deque(maxlen=LAG_WINDOW)
_lag_task: asyncio.Task | None = None

#: 保护 :func:`start` 的"注册 + 取/建执行器 + 启动"整段：并发调用时只会有一次真正装配，
#: 其余调用等在锁上，进来时直接复用已就绪的实例（``register_kinds`` 并发跑会因重复登记报错）。
_start_lock = threading.Lock()


#: 保护 :func:`register_kinds` 的"读注册表 → 发现 → 写注册表"整段。
#: 它可能被 :func:`start` 之外的路径直接调用（测试每个用例前一次），所以**不能只靠"启动时
#: 单线程调一次"的约定**：并发调用会各算一批 fresh、第二个 register_all 撞"已注册"报错。
_kinds_lock = threading.Lock()


def register_kinds() -> None:
    """发现并登记所有任务类型（幂等：跑几次都一样，不会重复也不会漏）。

    **重复调用是常态**：应用启动时一次、测试每个用例前一次（它们会先清空注册表）。
    因此这里必须能从零重建：发现过程不依赖 import 期副作用，每次调用都重新读模块上的声明。
    **并发安全**：整段加 :data:`_kinds_lock`（见那里的说明）。

    异常：
        ValueError: 声明不合规 / key 重复 / 与已注册的同名声明冲突。
    """
    with _kinds_lock:
        existing = {spec.key: spec for spec in api.kinds()}
        fresh = []
        for spec in discovered_kinds():
            registered = existing.get(spec.key)
            if registered is None:
                fresh.append(spec)
            elif registered != spec:
                # 同名但声明不同：两处代码都想当这个 kind 的主人，属于配置错误，早报错。
                raise ValueError(
                    f"任务类型 {spec.key!r} 已经注册过，且本次发现的声明与之不一致："
                    f"{registered!r} != {spec!r}"
                )
        if fresh:
            api.register_all(fresh)  # 原子：全合规才登记
            logger.debug("新登记 {} 种任务类型", len(fresh))
        logger.info("任务方法加载：{} → {}", __file__, [spec.key for spec in api.kinds()])


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


def start(heartbeat: Callable[[], None] | None = None) -> Runner:
    """启动任务执行器（应用入口在 `ensure_ready()` 之后调用）。

    参数：
        heartbeat: 可选心跳回调，挂在调度线程上（独立 worker 进程用于探活注入）。

    **可重复调用 + 线程安全**：整段加 :data:`_start_lock`；执行器走
    :meth:`Runner.singleton` 取/建（进程级唯一），`Runner.start()` 自己判"线程还活着就返回"。
    所以并发/重复调用都不会多起线程，也不会出现"两个实例互相覆盖、各自的
    ``recover_on_start`` 把对方在跑的任务打回 pending"。
    返回：进程级执行器（后续 `stop()` 用）。
    """
    with _start_lock:
        register_kinds()
        runner = Runner.singleton(lag_provider=lag_ms, heartbeat=heartbeat)
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
    """优雅退出：让在跑的任务收尾并放回队列（下次打开接着做）。

    **可重复调用**：`Runner.stop()` 会把线程置空，再调一次不会重复收尾。
    """
    runner = get_runner()
    if runner is not None:
        runner.stop(grace_s=grace_s)
