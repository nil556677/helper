"""调度与执行：把表里的任务真正跑起来。

**三条触发路径都在这**（`§13.5`）：投递即唤醒（:meth:`Runner.wake`）、**睡到下一条该跑的时刻**、
定时兜底（租约回收 / 保留清理 / 界面延迟看门狗）。它不是"固定间隔空转的定时扫描"。

**两条泳道**：``cpu`` 并发 1、``io`` 并发 2。依据是实测——4 个纯 Python 任务并发会让界面延迟
p95 到 113ms（一个只有 22ms），而 CPU 被吃满时受害的是**软件渲染的界面进程**。

**优雅退出**：先通知 handler（`JobContext.should_stop()` → 抛 `JobInterrupted`）→ 给一点宽限 →
仍未收尾的放回 `pending`（**不算失败**），下次启动接着做。
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from collections.abc import Callable
from uuid import uuid4

from loguru import logger

from app.core.jobs import registry, store
from app.core.jobs.model import (
    FAILED,
    Job,
    JobCancelled,
    JobContext,
    JobError,
    JobInterrupted,
)

#: 每条泳道的并发上限（cpu 必须 1：实测依据见模块说明）
LANE_LIMITS: dict[str, int] = {"cpu": 1, "io": 2}

#: **全局**同时执行的任务数上限（在泳道之上再加一道总闸）。
#: 泳道上限管的是"哪一类活儿能并几个"，这个管的是"整台机器上同时跑几个"——
#: 桌面场景下任务吃的是同一块磁盘与同一批 CPU 核，放开跑只会让界面和用户操作一起变慢。
#: 默认 3 = 现有泳道之和（cpu 1 + io 2），即"不改变现状的总量"，但从此有一个**明确、可见**的上限。
MAX_CONCURRENT_TASKS = 3

#: 有活可干时的轮询间隔；没活时最多睡这么久（等被 wake 或被下一条 next_run_at 叫醒）
ACTIVE_POLL_S = 0.5
IDLE_SLEEP_S = 5.0

#: 维护动作（租约回收 / 延迟看门狗）的间隔
MAINTENANCE_EVERY_S = 10.0

#: 保留清理的间隔（比维护稀得多：它只是别让历史无限长）
PURGE_EVERY_S = 600.0

#: 事件循环延迟超过它 → 暂停 cpu 泳道（"为保持界面流畅，向量化已让路"）
LAG_DEGRADE_MS = 200.0

#: 延迟检查的间隔（秒）。**故意比维护节拍短**：看门狗要快——否则一次短暂尖峰也会让 cpu 泳道
#: 停满 10 秒，而恢复也要等下一个 10 秒节拍（实测启动时就这样：466ms → 暂停 → 10 秒后恢复）。
LAG_CHECK_EVERY_S = 1.0

#: 连续几次都偏高才降级（去抖）：一次渲染尖峰不该让任务让路。
LAG_DEBOUNCE = 2

#: 有任务排了这么久还没轮到 → 在日志里喊一声（队列卡住时用户只会觉得"没反应"）
STUCK_WARN_S = 300.0

#: 心跳续租间隔（秒）。**与 handler 的 report() 无关**：有些 handler 一步就要跑几分钟
#: （解析大 PDF、全量重建），期间没有批次边界可调 report()——只靠 handler 续租，租约必然
#: 过期、任务被误判"卡死"回收，然后双跑。续租只看"线程还活着"，由调度线程按本节拍统一做
#: （见 :meth:`Runner._renew_leases`），不再单独养一条心跳线程。
HEARTBEAT_S = 5.0


class Runner:
    """任务执行器（**一个进程里只有一个**）。

    参数：
        inline: 测试模式——`tick()` 在调用线程里**同步**执行任务，不起任何线程（用例才确定）。
        lag_provider: 返回"事件循环最近延迟（毫秒 p95）"的回调；界面侧提供，超过
            :data:`LAG_DEGRADE_MS` 时 cpu 泳道自动让路。不传就不看。
        lane_limits: 覆盖默认并发上限（测试用）。
    """

    #: 进程级唯一实例（见 :meth:`singleton`）：所有启动路径共用它。
    _singleton: Runner | None = None
    #: 保护"判空 + 创建"（不加锁时两个线程会各建一个，各自起一条调度线程——后写的覆盖先写的，
    #: 而先写的线程还在跑，它的 recover_on_start 还会把对方在跑的任务打回 pending）。
    #: **RLock**：:meth:`singleton` 持锁调用构造器，而构造器里的"只许一个实例"检查也要这把锁。
    _singleton_lock = threading.RLock()

    @classmethod
    def singleton(cls, **kwargs: object) -> Runner:
        """取/建**进程级唯一**的执行器（线程安全）。

        为什么需要它：``start()`` 以前靠"调用约定"保证只调一次；一旦两个线程同时走到
        "读全局 → 判 None → 创建"，就会各建一个实例，后写的把先写的从 ``get_runner()``
        的视野里覆盖掉，而先写的调度线程仍在跑（还会把对方在跑的任务判成中断）。
        这里把"判空 + 创建"放进锁里，从机制上保证一个进程只有一个实例。

        参数：与 :meth:`__init__` 相同；**只有真正创建的那一次生效**（之后传入的参数被忽略）。
        返回：进程级唯一实例。
        """
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls(**kwargs)  # type: ignore[arg-type]
            return cls._singleton

    def __init__(
        self,
        *,
        inline: bool = False,
        lag_provider: Callable[[], float] | None = None,
        lane_limits: dict[str, int] | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        if not inline:
            # **机制**（而不是"请用 singleton() 的约定"）：非 inline 执行器一个进程只许有一个。
            # 直接构造第二个实例在这里就失败，而不是等两条调度线程都跑起来、互相把对方在跑的
            # 任务按"崩溃中断"打回 pending 才暴露（那种双跑很难排查）。
            # 用 RLock 是因为 :meth:`singleton` 会持锁再调 ``cls(...)``，同一线程需要可重入。
            with Runner._singleton_lock:
                if Runner._singleton is not None:
                    raise RuntimeError(
                        "进程内已有一个 Runner 实例：请用 Runner.singleton() 复用，"
                        "不要直接构造第二个（会导致双调度、任务双跑）"
                    )
        self.inline = inline
        self._lag_provider = lag_provider
        self._lane_limits = dict(lane_limits or LANE_LIMITS)
        #: 全局并发上限（见 :data:`MAX_CONCURRENT_TASKS`）
        self.max_concurrent = max(1, int(max_concurrent or MAX_CONCURRENT_TASKS))
        #: 本执行器的身份（进程号 + 随机后缀）：认领时写进 `worker`，退出收尾时据此只认自己的活
        self._tag = f"{os.getpid()}-{uuid4().hex[:6]}"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy: dict[str, int] = dict.fromkeys(self._lane_limits, 0)
        self._workers: list[threading.Thread] = []
        #: 本执行器正在跑的 job id（退出收尾时要区分"线程还活着"与"线程已经死了"）
        self._inflight: set[int] = set()
        #: 正在跑的 job → (worker 令牌, 租约秒数)：心跳线程据此续租
        self._leases: dict[int, tuple[str, float]] = {}
        self._lock = threading.Lock()
        #: 保护 :meth:`start` 的"判线程存活 → 建线程"整段（见那里的说明）。
        self._start_lock = threading.Lock()
        self._last_maintenance = 0.0
        self._last_purge = 0.0
        self._last_lag_check = 0.0
        self._lag_ms = 0.0
        self._lag_high_streak = 0
        self._warned_stuck = False
        self.lag_degraded = False
        """cpu 泳道是否因为界面延迟被自动让路（任务页会显示原因）。"""

    # ---------------- 生命周期 ----------------

    def _worker_name(self, lane: str) -> str:
        """认领者标识：**泳道 + 本执行器的身份**。

        为什么要带身份：光写泳道名（`cpu`）的话，多开两个窗口时两边认领的任务长得一模一样，
        关掉其中一个就会把**另一个正在跑的任务**也放回队列（它还在跑，于是同一个任务被跑两遍）。
        幂等虽然能兜住，但这属于"本来不该发生"的事。
        """
        return f"{lane}@{self._tag}"

    def start(self) -> None:
        """启动：先做崩溃恢复（**无条件**回收 running），再起调度线程。

        inline 模式只做恢复，不起线程（测试自己调 :meth:`tick`）。

        **顺序很关键**：非 inline 模式下先判"调度线程还活着就 return"，**再**做崩溃恢复。
        反过来（旧实现）会让重复调用 ``start()`` 把正在跑的任务改回 pending——它们的 worker
        线程还在写数据，随后又被认领一遍，就是双跑。

        **整段加锁**：判线程存活与建线程之间不能被切走，否则两个线程可能都判到"还没启动"、
        各起一条调度线程（多一条在后台跑，且 ``recover_on_start`` 会把对方在跑的任务打回 pending）。
        """
        with self._start_lock:
            if not self.inline and self._thread is not None and self._thread.is_alive():
                return
            outcome = store.recover_on_start()
            if outcome["recovered"] or outcome["failed"] or outcome.get("cancelled"):
                logger.info(
                    "任务恢复：{} 条接着做，{} 条判为反复崩溃，{} 条取消了",
                    outcome["recovered"],
                    outcome["failed"],
                    outcome.get("cancelled", 0),
                )
            store.purge_finished()
            self._stop.clear()
            if self.inline:
                return
            self._thread = threading.Thread(target=self._loop, name="jobs-scheduler", daemon=True)
            self._thread.start()
            logger.info("任务调度已启动（泳道并发 {}）", self._lane_limits)

    def request_stop(self) -> None:
        """只通知"要收了"：handler 在下一个批次边界抛 `JobInterrupted`（任务回队列，不算失败）。"""
        self._stop.set()
        self._wake.set()

    def stop(self, grace_s: float = 3.0) -> None:
        """优雅退出：通知 handler 收尾 → 宽限 → 仍在跑的回 `pending`（不算失败）。"""
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=grace_s)
            self._thread = None
        deadline = time.monotonic() + max(0.0, grace_s)
        while time.monotonic() < deadline:
            with self._lock:
                if not self._workers:
                    break
            time.sleep(0.05)
        with self._lock:
            still_running = len(self._inflight)
        if still_running:
            logger.warning("还有 {} 条任务没收尾（已放回队列或交给下次启动回收）", still_running)
        self._requeue_stragglers()
        # 收尾时尽量让 worker 线程结束：它们的**线程本地数据库连接**还开着，退出路径的
        # WAL checkpoint 会因为"还有连接在用库"而 busy（主库不完整）。这里只多等一小会儿；
        # 真卡在长操作里的线程是 daemon，进程退出时会被系统收走。
        with self._lock:
            stragglers = list(self._workers)
        for thread in stragglers:
            thread.join(timeout=0.5)
        logger.info("任务调度已停止（{}）", "队列空闲" if not still_running else f"在跑 {still_running} 条")

    def wake(self) -> None:
        """投递即唤醒：`enqueue` 之后调用，调度器立刻干活（毫秒级，不等下一个 tick）。"""
        self._wake.set()

    @property
    def running(self) -> bool:
        """调度线程是否在跑。"""
        return bool(self._thread and self._thread.is_alive())

    @property
    def running_count(self) -> int:
        """当前**正在执行**的任务数（不含排队）——界面显示的"并发 x/y"用它。"""
        with self._lock:
            return sum(self._busy.values())

    # ---------------- 调度 ----------------

    def _renew_leases(self) -> None:
        """给本执行器正在跑的任务续租（不依赖 handler 调 ``report()``）。

        由调度线程按 :data:`HEARTBEAT_S` 节拍调用（见 :meth:`_loop`）。
        为什么不单独养一条心跳线程：``_wait_next()`` 的睡眠上限就是 :data:`IDLE_SLEEP_S`
        （= 5s，与心跳间隔一致），调度线程每次醒来顺手做一次续租节拍完全够用，无需为这一件事
        再占用一条常驻线程；两种运行模式（同进程 / 独立 worker）都跟随 Runner 一起生效。
        """
        with self._lock:
            leases = dict(self._leases)
        for job_id, (worker, lease_s) in leases.items():
            try:
                store.renew_lease(job_id, lease_s=lease_s, worker=worker)
            except Exception:  # 续租失败不该拖垮调度（下一拍再试）
                logger.warning("任务 #{} 心跳续租失败", job_id)

    def tick(self, *, now: float | None = None) -> list[Job]:
        """认领一轮：每条泳道在有空位时把能跑的任务认领并派发。

        返回这一轮认领到的任务（inline 模式下已经**同步执行完**）。
        """
        claimed: list[Job] = []
        seen: set[int] = set()
        limits = self._lane_limits_now()
        for lane, kinds in self._lanes().items():
            limit = limits.get(lane, 0)
            while True:
                with self._lock:
                    if self._busy.get(lane, 0) >= limit:
                        break
                    # 全局总闸（泳道之上）：同时跑的任务总数不超过上限。
                    # inline 模式是同步执行、没有并发，这道闸对它没有意义（同泳道上限）。
                    if not self.inline and sum(self._busy.values()) >= self.max_concurrent:
                        break
                job = store.claim_next(
                    kinds, lease_s=self._lane_lease(lane), worker=self._worker_name(lane), now=now
                )
                if job is None:
                    break
                if job.id in seen:
                    # 一轮 tick 里同一个任务只处理一次：它刚被放回队列（可立即认领）时，
                    # 否则会"认领 → 中断 → 放回 → 再认领"转个不停（真踩过，一分钟 12 万行日志）
                    break
                seen.add(job.id)
                kind = self._kind_for(job)
                if kind is None:
                    continue
                claimed.append(job)
                if self.inline:
                    # inline 是**同步**执行：这里没有并发，泳道上限在这一模式下没有意义
                    # （真线程模式才需要），一轮 tick 内靠 seen 保证同一个任务不被反复认领
                    self._run_job(job, kind)
                else:
                    self._dispatch(job, kind)
        return claimed

    def drain(self, *, limit: int = 100, now: float | None = None) -> int:
        """inline 模式：把能跑的都跑完，返回**跑掉的任务数**（测试用；`now` 可传未来时间越过退避）。"""
        done = 0
        for _ in range(max(1, limit)):
            claimed = self.tick(now=now)
            if not claimed:
                break
            done += len(claimed)
        return done

    def maintenance(self, *, now: float | None = None) -> dict[str, int | bool]:
        """维护动作：回收过期租约 + 收拾"类型已不存在"的孤儿 + 保留清理 + 量界面延迟并按需让路。"""
        stamp = time.time() if now is None else now
        # 线程还活着的任务即使租约过期也不回收（它们可能正卡在一段没有 report 的长操作里，
        # 回收会让另一轮再认领它 → 双跑）。心跳线程会持续给它们续租。
        with self._lock:
            alive = tuple(self._inflight)
        outcome: dict[str, int | bool] = dict(store.reclaim_expired(now=stamp, alive=alive))
        outcome["orphans"] = self._fail_orphans()
        if stamp - self._last_purge >= PURGE_EVERY_S:
            outcome["purged"] = store.purge_finished(now=stamp)
            self._last_purge = stamp
        if self._lag_provider is not None:
            outcome["lag_ms"] = float(self._lag_provider())
        outcome["stuck"] = self._warn_if_stuck(stamp)
        self._last_maintenance = stamp
        return outcome

    def check_lag(self) -> bool:
        """量一次界面延迟，按需让路（每 :data:`LAG_CHECK_EVERY_S` 秒调一次）。

        去抖：**连续** :data:`LAG_DEBOUNCE` 次都偏高才降级（一次渲染尖峰不该让任务停），
        但只要一次正常就立刻恢复（恢复要快，别让用户白等）。

        返回：当前是否处于让路状态。
        """
        if self._lag_provider is None:
            return False
        lag = float(self._lag_provider())
        self._lag_ms = lag
        if lag > LAG_DEGRADE_MS:
            self._lag_high_streak += 1
        else:
            self._lag_high_streak = 0
        was_degraded = self.lag_degraded
        self.lag_degraded = self._lag_high_streak >= LAG_DEBOUNCE
        if self.lag_degraded and not was_degraded:
            logger.warning("界面延迟 {:.0f}ms 偏高，cpu 泳道暂停让路", lag)
        elif was_degraded and not self.lag_degraded:
            logger.info("界面延迟恢复正常（{:.0f}ms），cpu 泳道恢复", lag)
        return self.lag_degraded

    def _warn_if_stuck(self, stamp: float) -> bool:
        """队列卡住就喊一声（只喊一次，恢复后重新武装）。

        什么算卡住：有任务排了很久都没轮到。常见原因——队列被暂停了、某个泳道被长任务占着、
        或者 kind 忘了注册（`_fail_orphans` 会收拾最后这种）。不喊的话，用户只会觉得"没反应"。
        """
        oldest = store.oldest_pending_at()
        if oldest is None:
            self._warned_stuck = False
            return False
        waiting = stamp - oldest
        if waiting < STUCK_WARN_S:
            self._warned_stuck = False
            return False
        if not self._warned_stuck:
            logger.warning(
                "队列里有任务已等待 {:.0f} 分钟：检查是否暂停、泳道是否被长任务占满（任务 #详见任务页）",
                waiting / 60.0,
            )
            self._warned_stuck = True
        return True

    def _fail_orphans(self) -> int:
        """把"类型已经没注册"的待跑任务判掉。

        不收拾的话它们会一直躺在"排队"里：泳道只认注册过的 kind，永远轮不到它们被认领——
        用户看到一条永远不动的任务，却不知道为什么。
        """
        known = tuple(spec.key for spec in registry.registered())
        failed = 0
        for job in store.pending_of_unknown_kinds(known):
            store.fail(
                job.id,
                error=f"这个任务类型已经不存在了：{job.kind}",
                retryable=False,
                kind="unsupported",
            )
            failed += 1
        if failed:
            logger.warning("清理了 {} 条类型已不存在的任务", failed)
        return failed

    # ---------------- 内部 ----------------

    def _lanes(self) -> dict[str, tuple[str, ...]]:
        """泳道 → 该泳道的 kind（注册表是唯一来源：功能注册什么就跑什么）。"""
        lanes: dict[str, tuple[str, ...]] = {}
        for spec in registry.registered():
            lanes.setdefault(spec.lane, ())
            lanes[spec.lane] = (*lanes[spec.lane], spec.key)
        return lanes

    def _lane_lease(self, lane: str) -> float:
        """认领时用该泳道里**最长**的租约（安全侧；handler 每次 report 都会续租）。"""
        leases = [spec.lease_s for spec in registry.registered() if spec.lane == lane]
        return max(leases, default=60.0)

    def _lane_limits_now(self) -> dict[str, int]:
        """当前生效的并发上限（界面延迟高时 cpu 泳道让路 → 0）。"""
        limits = dict(self._lane_limits)
        if self.lag_degraded:
            limits["cpu"] = 0
        return limits

    def _kind_for(self, job: Job) -> registry.JobKind | None:
        """取任务对应的 kind；没注册过、或**参数版本对不上**就当场判失败。

        为什么要查 `payload_version`：程序升级后 payload 结构可能变了，而队列里还躺着旧格式的任务——
        直接跑会以奇怪的方式炸掉（或更糟：把错的参数当成对的用）。宁可明确判失败让用户重发。
        """
        try:
            spec = registry.get(job.kind)
        except KeyError as exc:
            logger.error("任务 #{} 的类型没有注册：{}", job.id, exc)
            store.fail(
                job.id,
                error=f"这个任务类型已经不存在了：{job.kind}",
                retryable=False,
                kind="unsupported",
            )
            return None
        if job.payload_version != spec.payload_version:
            logger.error(
                "任务 #{} 的参数版本不匹配：行内 v{}，代码 v{}",
                job.id,
                job.payload_version,
                spec.payload_version,
            )
            store.fail(
                job.id,
                error="这条任务是用旧版本排的（参数格式变过），请重新发起一次",
                retryable=False,
                kind="unsupported",
            )
            return None
        return spec

    def _dispatch(self, job: Job, kind: registry.JobKind) -> None:
        """派发到一个工作线程（泳道并发靠 `_busy` 计数控制）。"""
        worker = self._worker_name(kind.lane)
        with self._lock:
            self._busy[kind.lane] = self._busy.get(kind.lane, 0) + 1
            self._inflight.add(job.id)
            # 心跳线程据此续租：间隔与 handler 是否 report() 无关
            self._leases[job.id] = (worker, kind.lease_s)

        def work() -> None:
            try:
                self._run_job(job, kind, worker=worker)
            finally:
                with self._lock:
                    self._busy[kind.lane] = max(0, self._busy.get(kind.lane, 1) - 1)
                    self._inflight.discard(job.id)
                    self._leases.pop(job.id, None)
                    if threading.current_thread() in self._workers:
                        self._workers.remove(threading.current_thread())
                # 空出一个位置就**立刻叫醒调度器**去认领下一条：不然它要等到下一次轮询
                # （泳道占满时那 0.5 秒的空转也就没必要了）
                self.wake()

        thread = threading.Thread(target=work, name=f"jobs-{kind.key}-{job.id}", daemon=True)
        with self._lock:
            self._workers.append(thread)
        thread.start()

    def _run_job(self, job: Job, kind: registry.JobKind, *, worker: str = "") -> None:
        """跑一个任务并**把结果落到状态里**（成功 / 取消 / 中断 / 失败四条路）。

        ``worker``：认领者令牌；所有收尾写入都带上它——任务被回收后旧线程的写入会被忽略。
        """
        worker_token = worker or None
        context = JobContext(
            job_id=job.id,
            kind=job.kind,
            payload=job.payload,
            cursor=job.cursor,
            worker=worker,
            write_progress=lambda done, total, note: store.write_progress(
                job.id, done, total, note, lease_s=kind.lease_s, worker=worker_token
            ),
            write_cursor=lambda value: store.set_cursor(job.id, value, worker=worker_token),
            is_cancelled=lambda: store.is_cancelled(job.id),
            should_stop=lambda: self._stop.is_set(),
            is_paused=store.get_paused,
        )
        started = time.perf_counter()
        try:
            note = kind.handler(context) or ""
        except JobCancelled:
            context.flush()
            if kind.on_cancel is not None:
                try:
                    kind.on_cancel(context)
                except Exception:
                    logger.exception("任务 #{} 的 on_cancel 清理失败", job.id)
            store.settle_cancelled(job.id, worker=worker_token)
            logger.info("任务 #{}（{}）已取消", job.id, kind.key)
        except JobInterrupted:
            context.flush()
            store.requeue_interrupted(job.id, worker=worker_token)
            logger.info("任务 #{}（{}）因退出中断，已放回队列", job.id, kind.key)
        except JobError as exc:
            self._fail(
                job, kind, error=str(exc), retryable=exc.retryable,
                error_kind=exc.kind, worker=worker_token,
            )
        except Exception as exc:
            logger.exception("任务 #{}（{}）崩了", job.id, kind.key)
            # 没预料到的异常：**把 traceback 也存下来**（只进日志的话，用户从界面上找不到它）
            self._fail(
                job,
                kind,
                error=f"内部错误：{exc}",
                retryable=True,
                error_kind="unknown",
                detail=traceback.format_exc(),
                worker=worker_token,
            )
        else:
            context.flush()
            store.finish(job.id, note=str(note), worker=worker_token)
            logger.info(
                "任务 #{}（{}）完成，用时 {:.2f}s", job.id, kind.key, time.perf_counter() - started
            )

    def _fail(
        self,
        job: Job,
        kind: registry.JobKind,
        *,
        error: str,
        retryable: bool,
        error_kind: str = "unknown",
        detail: str = "",
        worker: str | None = None,
    ) -> None:
        """落失败状态。**不幂等的 kind 一律不重试**（重跑会造成重复副作用）。"""
        allowed = retryable and kind.idempotent
        new_status = store.fail(
            job.id,
            error=error,
            retryable=allowed,
            kind=error_kind,
            detail=detail,
            worker=worker,
        )
        if new_status == "stale":
            logger.warning("任务 #{}（{}）的失败写入被忽略（这一轮已被回收）", job.id, kind.key)
            return
        logger.info(
            "任务 #{}（{}）失败：{}（{} · {}）",
            job.id,
            kind.key,
            error,
            error_kind,
            "不再重试" if new_status == FAILED else "等退避重试",
        )

    def _requeue_stragglers(self) -> None:
        """宽限期到了还没收尾的：放回 `pending`（可能重跑，幂等由 kind 自己保证）。

        **只放回两类之外的自己人**：① 本执行器认领的（worker 里带身份后缀）；
        ② **线程已经不在跑了的**——线程还活着说明它可能正在写数据，这时把任务改回 `pending`
        会让另一个执行器/下一轮把它再认领一遍（真的双跑）。活着的那些留给"下次启动的无条件回收"，
        反正结果一样（进程退出时它们必死）。
        """
        with self._lock:
            alive = set(self._inflight)
        rows = store.list_jobs(statuses=("running",), limit=200)
        for job in rows:
            if not (job.worker or "").endswith(f"@{self._tag}"):
                continue
            if job.id in alive:
                logger.warning("任务 #{} 的线程还在跑，交给下次启动回收（不在退出时改状态）", job.id)
                continue
            store.requeue_interrupted(job.id, note="应用退出时仍在跑，下次接着做")
            logger.warning("任务 #{} 在宽限期内没收尾，已放回队列", job.id)

    def _wait_next(self) -> None:
        """睡到下一条该跑的时刻（没有就睡 IDLE_SLEEP_S），被 `wake()` 提前叫醒。

        这就是"不是固定间隔空转"的实现：退避重试精确睡到点，投递则毫秒级被叫醒。
        """
        due = store.next_due_at()
        if due is None:
            delay = IDLE_SLEEP_S
        elif due <= time.time():
            delay = ACTIVE_POLL_S  # 有活等着（或泳道占满），按活跃间隔轮询
        else:
            delay = min(due - time.time(), IDLE_SLEEP_S)
        self._wake.wait(timeout=max(0.05, delay))
        self._wake.clear()

    def _loop(self) -> None:
        """调度线程主循环。"""
        last_renew = 0.0
        while not self._stop.is_set():
            try:
                if not store.get_paused():
                    self.tick()
                if time.time() - self._last_maintenance >= MAINTENANCE_EVERY_S:
                    self.maintenance()
                if time.time() - self._last_lag_check >= LAG_CHECK_EVERY_S:
                    self._last_lag_check = time.time()
                    self.check_lag()
                # 续租与暂停/忙碌无关：在跑的任务无论队列状态如何都要保活（独立线程已被并入这里）。
                now = time.time()
                if now - last_renew >= HEARTBEAT_S:
                    self._renew_leases()
                    last_renew = now
            except Exception:
                logger.exception("调度循环出错（继续跑）")
            self._wait_next()


def get_runner() -> Runner | None:
    """当前的进程级执行器（没创建过就是 None）。"""
    return Runner._singleton
