"""``settings`` 表的键值读写（**通用 KV**：界面偏好、队列暂停、启动体检标记…）。

**为什么单独一个模块**：这张表被三个域用到（界面偏好 / 任务队列 / 启动体检），
SQL 与"值用 JSON 存"的约定原先各写一遍——改一处忘一处就会读到解不开的值。
读写都走这里，约定只有一份。

**值一律是 JSON**（``json.dumps``）：既能存字符串也能存对象，读坏了按"没设置"处理
（偏好丢了不该让程序起不来）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from app.core.db.connection import get_connection, transaction


def read_json(key: str) -> Any | None:
    """读一条；没有或值坏了解不开都返回 ``None``（按"没设置"处理）。"""
    row = get_connection().execute(
        "SELECT value_json FROM settings WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(str(row["value_json"]))
    except ValueError as exc:
        logger.warning("设置项 {} 的值不是合法 JSON，按未设置处理：{}", key, exc)
        return None


def write_json(key: str, value: Any) -> None:
    """写一条（存在就覆盖）。值走 JSON 编码。"""
    payload = json.dumps(value, ensure_ascii=False)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
            " updated_at = excluded.updated_at",
            (key, payload, time.time()),
        )


def delete(key: str) -> None:
    """删一条（例如"这一轮还没结束"要把上一轮的干净退出标记清掉）。"""
    get_connection().execute("DELETE FROM settings WHERE key = ?", (key,))
