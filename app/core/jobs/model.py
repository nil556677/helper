"""任务的数据形状与契约（**这里没有 SQL**：SQL 全在 :mod:`app.core.jobs.store`）。

三件东西在这里定死，因为整个系统都依赖它们的语义：

- **状态常量**：``pending / running / done / failed / cancelled``；
- **失败与中断是两回事**：``JobError`` 是"这件事失败了"（可以重试），``JobCancelled`` 是
  "用户不要了"，``JobInterrupted`` 是"应用要关了"——后两者**不算失败**，也不该耗掉重试机会；
- **``JobContext`` 是 handler 的唯一接口**：写进度、续租、查取消三件事合成一个 ``report()``，
  写 handler 的人不可能只做对一件、忘了另一件。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: 状态
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

#: 还"活着"的状态（占着 dedupe 键、会出现在任务页顶部）
ACTIVE_STATUSES = (PENDING, RUNNING)

#: 已经结束的状态（保留策略清理的对象）
FINISHED_STATUSES = (DONE, FAILED, CANCELLED)

#: 优先级三档（数值越大越先跑）
PRIORITY_WAITING = 10
"""用户点了按钮、正盯着进度看（导入、手动重试）。"""
PRIORITY_DEFAULT = 0
"""默认：后台补课（向量化新导入的资料）。"""
PRIORITY_MAINTENANCE = -10
"""维护类：清理、校验、回收孤儿副本——绝不和用户的活抢。"""

#: 这些失败**不值得重试**（复用 §10.1 的九类失败里"填错了"的那几类）
#: key 错、地址错、模型名错：再试 3 次只是在浪费用户的时间。
NO_RETRY_KINDS = frozenset({"config", "auth", "not_found", "bad_request", "unsupported"})


class JobError(RuntimeError):
    """handler 明确的失败。

    参数：
        message: 给人看的一句话（会直接显示在任务页与日志里）。
        kind: 失败分类（默认 ``unknown``）——取 §10.1 的九类之一时能自动决定要不要重试。
        retryable: 显式覆盖"要不要重试"；不传就按 ``kind`` 判定。
    """

    def __init__(self, message: str, *, kind: str = "unknown", retryable: bool | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = (kind not in NO_RETRY_KINDS) if retryable is None else retryable


class JobCancelled(RuntimeError):
    """用户取消了任务：任务收尾为 ``cancelled``，**不算失败**。"""


class JobInterrupted(RuntimeError):
    """应用要退出了：任务收尾回 ``pending``，下次启动接着做，**不算失败**。"""


def _payload_from_json(raw: str) -> dict[str, Any]:
    """解析 payload（坏 JSON 不让整个任务页崩掉）。"""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Job:
    """表里的一行任务。"""

    id: int
    kind: str
    payload: dict[str, Any]
    payload_version: int
    status: str
    priority: int
    progress: float
    done: int
    total: int
    note: str
    runs: int
    fails: int
    max_attempts: int
    cursor: str | None
    resource: str | None
    cancel_requested: bool
    next_run_at: float
    lease_until: float | None
    worker: str | None
    error: str | None
    error_kind: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None
    updated_at: float

    @classmethod
    def from_row(cls, row: Any) -> Job:
        """从 sqlite3.Row 构造（缺列时给安全默认值，避免以后加列就炸）。"""
        keys = set(row.keys())

        def value(name: str, default: Any = None) -> Any:
            return row[name] if name in keys else default

        return cls(
            id=int(row["id"]),
            kind=str(row["kind"]),
            payload=_payload_from_json(str(value("payload", "{}"))),
            payload_version=int(value("payload_version", 1) or 1),
            status=str(row["status"]),
            priority=int(value("priority", 0) or 0),
            progress=float(value("progress", 0.0) or 0.0),
            done=int(value("done", 0) or 0),
            total=int(value("total", 0) or 0),
            note=str(value("note", "") or ""),
            runs=int(value("runs", 0) or 0),
            fails=int(value("fails", 0) or 0),
            max_attempts=int(value("max_attempts", 3) or 3),
            cursor=value("cursor"),
            resource=value("resource"),
            cancel_requested=bool(value("cancel_requested", 0)),
            next_run_at=float(value("next_run_at", 0.0) or 0.0),
            lease_until=value("lease_until"),
            worker=value("worker"),
            error=value("error"),
            error_kind=value("error_kind"),
            created_at=float(value("created_at", 0.0) or 0.0),
            started_at=value("started_at"),
            finished_at=value("finished_at"),
            updated_at=float(value("updated_at", 0.0) or 0.0),
        )

    @property
    def active(self) -> bool:
        """还在队列里（排队中或在跑）。"""
        return self.status in ACTIVE_STATUSES

    @property
    def cancelled_pending(self) -> bool:
        """已经请求取消、但还在跑（界面显示「正在取消…」）。"""
        return self.status == RUNNING and self.cancel_requested


@dataclass(frozen=True)
class JobEvent:
    """任务身上发生过的一件事（失败 / 中断 / 取消 / 崩溃恢复）。

    **为什么要它**：`jobs.error` 只留得住最后一次失败，而可重试的任务会失败好几次——
    排查时要看的是时间线：哪几次失败、每次什么分类、有没有 traceback。
    """

    id: int
    job_id: int
    at: float
    attempt: int
    kind: str
    message: str
    detail: str

    @classmethod
    def from_row(cls, row: Any) -> JobEvent:
        """从 sqlite3.Row 构造。"""
        return cls(
            id=int(row["id"]),
            job_id=int(row["job_id"]),
            at=float(row["at"]),
            attempt=int(row["attempt"] or 0),
            kind=str(row["kind"]),
            message=str(row["message"] or ""),
            detail=str(row["detail"] or ""),
        )


@dataclass
class JobContext:
    """handler 拿到的上下文。

    三个回调由引擎注入（写进度、查取消、查停止），handler 只要**周期性调用** :meth:`report`：
    一次调用同时完成"写进度 / 续租 / 响应取消"，三者不可能只做对一件。

    参数：
        job_id: 任务 id。
        kind: 任务类型。
        payload: 任务参数（只放引用，不放 bytes）。
        cursor: 断点续跑位置（长任务用；没有就是 None）。
        write_progress: 引擎注入：``(done, total, note) -> None``。
        write_cursor: 引擎注入：``(value) -> None``。
        is_cancelled: 引擎注入：用户是否请求取消。
        should_stop: 引擎注入：应用是否要退出。
        throttle_s: 进度写库的最小间隔（默认 0.2 秒）——10 万次写库是不可接受的。
    """

    job_id: int
    kind: str
    payload: dict[str, Any]
    cursor: str | None
    write_progress: Callable[[int, int, str], None]
    write_cursor: Callable[[str], None]
    is_cancelled: Callable[[], bool]
    should_stop: Callable[[], bool]
    is_paused: Callable[[], bool] = lambda: False
    throttle_s: float = 0.2
    pause_poll_s: float = 0.2
    renew_s: float = 5.0
    _last_write: float = field(default=0.0, init=False)
    _last_renew: float = field(default=0.0, init=False)
    _latest: tuple[int, int, str] | None = field(default=None, init=False)

    @property
    def progress(self) -> float:
        """最近一次报的进度（0~1）。"""
        if not self._latest or not self._latest[1]:
            return 0.0
        return min(1.0, max(0.0, self._latest[0] / self._latest[1]))

    def set_cursor(self, value: str) -> None:
        """记下断点续跑位置（立刻落库：它就是为"崩了以后接着跑"准备的）。"""
        self.cursor = value
        self.write_cursor(value)

    def report(self, done: int, total: int, note: str = "") -> None:
        """写进度 + 续租 + 检查取消/停止/暂停（**handler 必须周期性调用**）。

        队列被暂停时**在这里挂起**（批次边界）——这是"暂停"对正在跑的任务的含义：
        不再往前走，但也不丢（租约照续，不会被判卡死回收）。

        异常：
            JobCancelled: 用户请求了取消。
            JobInterrupted: 应用要退出（任务回到 pending，下次接着做）。
        """
        self._check()
        self._latest = (done, total, note)
        # 节流：进度按批写，不逐条写（10 万次写库是不可接受的）
        if time.monotonic() - self._last_write >= self.throttle_s:
            self.flush()
        while self.is_paused():
            self._check()
            time.sleep(self.pause_poll_s)
            if time.monotonic() - self._last_renew >= self.renew_s:
                self.flush()  # 续租：否则会被判"卡死"回收，变成同一个任务跑两遍

    def flush(self) -> None:
        """把最近一次进度真正写下去（节流可能压住了它；收尾前一定要调）。"""
        if self._latest is None:
            return
        done, total, note = self._latest
        self.write_progress(done, total, note)
        now = time.monotonic()
        self._last_write = now
        self._last_renew = now

    def _check(self) -> None:
        """取消与退出：每到一个批次边界都要看。"""
        if self.is_cancelled():
            raise JobCancelled(f"任务 {self.job_id} 被取消")
        if self.should_stop():
            raise JobInterrupted(f"任务 {self.job_id} 因应用退出而中断")
