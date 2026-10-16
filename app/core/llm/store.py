"""``llm_slots`` 表的读写（**槽位配置的唯一出入库口**）。

为什么单独一个模块：模型槽位的 SQL 原先写在 `app/services/models.py` 里（服务层直接拼 SQL），
与 `documents`/`chunks`（`ingest/store.py`）、`jobs`（`jobs/store.py`）、`config`（`config/store.py`）
不一致。收进来之后**一域一口**，改字段只需改这里。

**三种写入语义必须分开表达**（这是这张表最容易写错的地方）：

- :func:`upsert_meta`：只改手填三项，**绝不碰密钥列**（"没换 key 就别动它"）；
- :func:`upsert_with_key`：三项 + 新的密文与尾号；
- :func:`clear_key`：三项 + 把密钥列**置空**（用户主动清掉 key）。

**api key 只以密文入库**（DPAPI，见 `core/security`）：本模块只管存取字节，
加解密由调用方在写入那一刻做——明文绝不经过这里。
"""

from __future__ import annotations

import time

from app.core.db.connection import get_connection, transaction


def get(slot: str) -> object | None:
    """取一个槽位那一行；没有返回 ``None``。"""
    return get_connection().execute(
        "SELECT * FROM llm_slots WHERE slot = ?", (slot,)
    ).fetchone()


def list_all() -> list[object]:
    """全部槽位行（界面按 ``SLOTS`` 的顺序自己排，这里不负责顺序）。"""
    return list(get_connection().execute("SELECT * FROM llm_slots").fetchall())


def upsert_meta(
    slot: str, *, provider: str, base_url: str, model: str, now: float | None = None
) -> None:
    """只写手填三项（**密钥列原样不动**）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        conn.execute(
            "INSERT INTO llm_slots(slot, provider, base_url, model, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(slot) DO UPDATE SET provider = excluded.provider,"
            " base_url = excluded.base_url, model = excluded.model,"
            " updated_at = excluded.updated_at",
            (slot, provider, base_url, model, stamp),
        )


def upsert_with_key(
    slot: str,
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key_cipher: bytes,
    key_tail: str,
    now: float | None = None,
) -> None:
    """写手填三项 + 新的密文与尾号。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        conn.execute(
            "INSERT INTO llm_slots(slot, provider, base_url, model, api_key_cipher, key_tail,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(slot) DO UPDATE SET provider = excluded.provider,"
            " base_url = excluded.base_url, model = excluded.model,"
            " api_key_cipher = excluded.api_key_cipher, key_tail = excluded.key_tail,"
            " updated_at = excluded.updated_at",
            (slot, provider, base_url, model, api_key_cipher, key_tail, stamp),
        )


def clear_key(
    slot: str, *, provider: str, base_url: str, model: str, now: float | None = None
) -> None:
    """写手填三项 + 把密钥列**置空**（用户主动清掉 key，尾号也一起清）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        conn.execute(
            "INSERT INTO llm_slots(slot, provider, base_url, model, api_key_cipher, key_tail,"
            " updated_at) VALUES (?, ?, ?, ?, NULL, NULL, ?)"
            " ON CONFLICT(slot) DO UPDATE SET provider = excluded.provider,"
            " base_url = excluded.base_url, model = excluded.model,"
            " api_key_cipher = NULL, key_tail = NULL, updated_at = excluded.updated_at",
            (slot, provider, base_url, model, stamp),
        )
