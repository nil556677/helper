"""启动时数据库体检（只在"上次没干净退出"时做，T3.2）。

**为什么只在非干净退出时体检**：每次启动都扫库没必要，而且对用户来讲"启动慢"是明确痛点。
所以用三个事实凑出一套逻辑：

1. 干净退出时写一个标记（``settings.last_shutdown_clean = <时间戳>``）；
2. 启动时读到这个标记 → 说明上次是干净退出的 → 跳过体检；并把标记**清掉**
   （表示"这一轮还没结束"，避免拿旧标记永远跳过）；
3. 读不到标记 → 上次没干净退出（或从没干净退出过）→ 跑 ``PRAGMA quick_check``。

**只读性**：体检本身只查不修；发现损坏时由调用方（``app/__main__.py``）决定
怎么提示与恢复（从备份恢复 / 重命名坏库重建）。本模块不删数据。
"""

from __future__ import annotations

import json
import time

from loguru import logger

from app.core.db.connection import get_connection, transaction

#: settings 表里的键。
KEY_CLEAN_EXIT = "health_last_shutdown_clean"


def mark_clean_exit() -> None:
    """把"本次是干净退出"写进 settings（``on_quit``/``atexit`` 里调）。

    ``value_json`` 按整表约定存 JSON（见 jobs 的暂停状态的写法）。
    """
    with transaction() as conn:
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
            " updated_at = excluded.updated_at",
            (KEY_CLEAN_EXIT, json.dumps({"shutdown_clean": True, "at": time.time()}), time.time()),
        )


def _read_clean_mark() -> float | None:
    """读上次的干净退出标记；没有/坏了返回 None。"""
    row = get_connection().execute(
        "SELECT value_json FROM settings WHERE key = ?", (KEY_CLEAN_EXIT,)
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["value_json"])
    except (TypeError, ValueError):
        return None
    at = payload.get("at") if isinstance(payload, dict) else None
    return float(at) if at is not None else None


def _clear_clean_mark() -> None:
    """把标记清掉：表示"这一轮还没结束"。启动读取后立刻清。"""
    get_connection().execute("DELETE FROM settings WHERE key = ?", (KEY_CLEAN_EXIT,))


def last_shutdown_was_clean() -> bool:
    """上次是不是干净退出（读标记后**清掉**，表示本轮进行中）。

    返回：
        ``True`` = 上次干净退出；``False`` = 上次没干净退出 / 从未记录。
    副作用：清除标记（读一次即失效，避免旧标记永远跳过体检）。
    """
    mark = _read_clean_mark()
    _clear_clean_mark()
    return mark is not None


def quick_check() -> str:
    """跑 ``PRAGMA quick_check``，返回一致性原文（``ok`` 之外都算有问题）。"""
    row = get_connection().execute("PRAGMA quick_check").fetchone()
    return str(row[0]) if row else "?"


def verify_unclean_exit() -> str | None:
    """体检入口：上次没干净退出就做一次 quick_check。

    返回：一致时 ``None``；发现问题时返回一致性原文（供调用方写 startup-error + 弹框）。
    """
    if last_shutdown_was_clean():
        logger.debug("上次是干净退出，跳过数据库体检")
        return None
    logger.info("上次未干净退出，执行数据库体检…")
    result = quick_check()
    if result == "ok":
        logger.info("上次未干净退出，数据库体检通过")
        return None
    logger.error("数据库体检未通过：quick_check 返回 {}", result)
    return result
