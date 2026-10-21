"""共享私有实现：常量与通用帮助（各子模块共用，避免循环依赖）。

这里放的事件截断/写入是被 ``jobs``、``events``、``lease`` 三个子模块共同依赖的私有工具，
单独拎出来是因为它们互相 import 时谁都不能在带工具的那一方之前被加载。
"""

from __future__ import annotations

import time
from typing import Any

from app.core.jobs.model import PENDING, RUNNING

#: 事件表里单条文本的长度上限（traceback 可能几万字符，事件表不该被一条记录撑爆）
MESSAGE_MAX_CHARS = 500
DETAIL_MAX_CHARS = 8000

#: 连续崩溃（认领了却没好好结束）超过它 → 判"反复崩溃"，不再重跑
CRASH_LIMIT = 5

#: 退避：``base × 2^(fails-1)``，上限 5 分钟
BACKOFF_BASE_S = 5.0
BACKOFF_MAX_S = 300.0

#: 保留策略：完成的任务留 7 天，且至少留最近 200 条
KEEP_FINISHED_ROWS = 200
KEEP_FINISHED_DAYS = 7.0

#: 队列暂停状态存的键（settings 表；JSON 编码，见 :mod:`app.core.db.connection` 的写入约定）
PAUSED_KEY = "jobs.paused"

#: 状态排序：任务页按"正在跑 → 排队 → 已结束"排
_STATUS_ORDER = f"CASE status WHEN '{RUNNING}' THEN 0 WHEN '{PENDING}' THEN 1 ELSE 2 END"


def _clip(text: str, *, limit: int = MESSAGE_MAX_CHARS) -> str:
    """截断过长的文本（traceback 可能几万字符；事件表不该被一条记录撑爆）。"""
    text = str(text or "")
    return text if len(text) <= limit else f"{text[:limit]}…（已截断）"


def _insert_event(
    conn: Any,
    job_id: int,
    *,
    kind: str,
    message: str,
    detail: str = "",
    attempt: int = 0,
    now: float | None = None,
) -> None:
    """往事件表写一笔（**必须在调用方的事务里**：状态与事件要么一起成、要么一起不成）。"""
    conn.execute(
        "INSERT INTO job_events(job_id, at, attempt, kind, message, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (
            job_id,
            time.time() if now is None else now,
            int(attempt),
            kind,
            _clip(message),
            _clip(detail, limit=DETAIL_MAX_CHARS),
        ),
    )
