"""``documents`` 与 ``chunks`` 的**唯一出入库口**（域内所有 SQL 都在这儿）。

为什么必须唯一（两条都由真实故障逼出来）：

1. **chunks 的 ``text`` 与 ``tokens`` 必须同步写**：FTS5 索引的是 ``tokens``、正文在 ``text``，
   两列一旦不同步就是"看得见正文、搜不到内容"——而且**不会报错**，最难查。所以插入只走这里：
   索引文本在函数内部算，调用方**没有机会漏**。
2. **documents 的读写原先散在 pipeline 与多个 service 里**（同一张表的 SQL 出现在三处），
   改一个字段要满仓库找。现在统一收进来，与 ``jobs/store.py``、``config/store.py`` 一个模式。

**事务约定**（照抄既有风格，别混）：

- 形参有 ``conn`` 的 → **不自己开事务**，由调用方决定边界（例如"插资料 + 写片段"要同一个事务）；
- 其余单个逻辑写的 → 自己开事务（``record_failure`` / ``mark_failed`` / ``refresh_file_hash``）。

读函数返回 ``sqlite3.Row``（core 内部允许字符串键；**跨层到 service 时才映射成视图模型**）。
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from app.core.db.connection import get_connection, transaction
from app.core.retrieval.tokenize import to_tokens


def write_chunks(conn: object, doc_id: int, texts: Sequence[str], *, now: float | None = None) -> int:
    """写入一份资料的片段（``text`` 与 ``tokens`` 一起算、一起写）。

    参数：
        conn: 已打开的连接（调用方负责事务边界）。
        doc_id: 所属资料 id。
        texts: 片段正文，顺序即 ``seq``。
        now: 时间戳（默认当前时间；测试里传固定值）。
    返回：写入的片段条数。
    """
    stamp = time.time() if now is None else now
    rows = [(doc_id, seq, text, to_tokens(text), stamp) for seq, text in enumerate(texts)]
    conn.executemany(  # type: ignore[attr-defined]
        "INSERT INTO chunks(doc_id, seq, text, tokens, created_at) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def replace_chunks(conn: object, doc_id: int, texts: Sequence[str], *, now: float | None = None) -> int:
    """**重建**用：先删掉这份资料的全部片段，再写新的（调用方放在同一个事务里）。

    参数：
        conn: 已打开的连接。
        doc_id: 资料 id。
        texts: 新的片段正文。
        now: 时间戳。
    返回：新写入的片段条数。
    """
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))  # type: ignore[attr-defined]
    return write_chunks(conn, doc_id, texts, now=now)


# ---------------------------------------------------------------------------
# documents：写
# ---------------------------------------------------------------------------


def find_id_by(column: str, value: str) -> int | None:
    """按某一列判重（``file_hash`` / ``text_hash``），返回最早那条的 id。

    **列名由调用方给**（两个固定的判重列），值一律走占位符——不存在拼接注入。
    """
    row = get_connection().execute(
        f"SELECT id FROM documents WHERE {column} = ? ORDER BY id LIMIT 1", (value,)
    ).fetchone()
    return int(row["id"]) if row else None


def refresh_file_hash(doc_id: int, file_hash: str, *, now: float | None = None) -> None:
    """正文判重命中时把新文件的字节哈希补上（下次这份文件走最快的路径）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        conn.execute(
            "UPDATE documents SET file_hash = ?, updated_at = ? WHERE id = ?",
            (file_hash, stamp, doc_id),
        )


def record_failure(name: str, size: int, reason: str, *, now: float | None = None) -> int:
    """把失败也记进资料表（用户在列表里能看到它、也能删掉它）。返回新的 doc id。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO documents(source_name, stored_path, size, status, detail, created_at,"
            " updated_at) VALUES (?, '', ?, 'failed', ?, ?, ?)",
            (name, size, reason, stamp, stamp),
        )
        return int(cursor.lastrowid or 0)


def insert_ready(
    conn: object,
    *,
    source_name: str,
    stored_path: str,
    file_hash: str,
    text_hash: str,
    size: int,
    detail: str | None,
    now: float | None = None,
) -> int:
    """插一条"就绪"的资料（**不自己开事务**：调用方接着还要 ``write_chunks``）。

    返回：新的 doc id。
    """
    stamp = time.time() if now is None else now
    cursor = conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO documents(source_name, stored_path, file_hash, text_hash, size, status,"
        " detail, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?)",
        (source_name, stored_path, file_hash, text_hash, size, detail, stamp, stamp),
    )
    return int(cursor.lastrowid or 0)


def mark_failed(doc_id: int, detail: str, *, now: float | None = None) -> None:
    """把一份资料标成失败（原因写进 ``detail``，界面上能看到）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        conn.execute(
            "UPDATE documents SET status = 'failed', detail = ?, updated_at = ? WHERE id = ?",
            (detail, stamp, doc_id),
        )


def mark_ready(
    conn: object,
    doc_id: int,
    *,
    text_hash: str,
    detail: str | None,
    now: float | None = None,
) -> None:
    """把一份资料标成就绪（重建路径用；**不自己开事务**，要和 ``replace_chunks`` 同事务）。"""
    stamp = time.time() if now is None else now
    conn.execute(  # type: ignore[attr-defined]
        "UPDATE documents SET status = 'ready', text_hash = ?, detail = ?, updated_at = ?"
        " WHERE id = ?",
        (text_hash, detail, stamp, doc_id),
    )


def delete_many(doc_ids: Sequence[int]) -> list[str]:
    """删掉若干份资料（片段与索引由外键级联 + 触发器一起清）。

    返回：被删资料的 ``stored_path``（调用方据此清副本目录）——**副本是文件系统的事，
    存储层只负责库**。
    """
    ids = [int(doc_id) for doc_id in doc_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with transaction() as conn:
        rows = conn.execute(
            f"SELECT id, stored_path FROM documents WHERE id IN ({placeholders})", tuple(ids)
        ).fetchall()
        conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", tuple(ids))
    return [str(row["stored_path"] or "") for row in rows]


# ---------------------------------------------------------------------------
# documents：读
# ---------------------------------------------------------------------------

#: 列表/分页共用的查询（分页与"全都要"共用一段 SELECT，避免两处漂移）
_LIST_SELECT = """
        SELECT d.id AS id, d.source_name AS name, d.size AS size, d.status AS status,
               d.detail AS detail, d.updated_at AS updated_at,
               (SELECT count(*) FROM chunks c WHERE c.doc_id = d.id) AS chunks
        FROM documents d
"""


def get(doc_id: int) -> object | None:
    """按 id 取一行（重建要用它的 ``source_name`` 与 ``stored_path``）。

    返回：``sqlite3.Row``；不存在返回 ``None``。
    """
    return get_connection().execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()


def all_stored_paths() -> list[str]:
    """全部资料的副本相对路径（清理孤儿副本时用它算"哪些还被引用着"）。"""
    rows = get_connection().execute(
        "SELECT stored_path FROM documents WHERE stored_path IS NOT NULL AND stored_path != ''"
    ).fetchall()
    return [str(row["stored_path"]) for row in rows]


def counts() -> object:
    """概览那一行计数（就绪份数 / 失败份数 / 片段数 / 就绪占用字节 / 最后更新时间）。"""
    return get_connection().execute(
        """
        SELECT
          (SELECT count(*) FROM documents WHERE status = 'ready') AS documents,
          (SELECT count(*) FROM documents WHERE status = 'failed') AS failed,
          (SELECT count(*) FROM chunks) AS chunks,
          (SELECT coalesce(sum(size), 0) FROM documents WHERE status = 'ready') AS bytes,
          (SELECT max(updated_at) FROM documents WHERE status = 'ready') AS updated_at
        """
    ).fetchone()


def list_all() -> list[object]:
    """全部资料（新的在前）。列表页走 :func:`page` 分页，这个给测试与全量统计用。"""
    return list(
        get_connection().execute(
            f"{_LIST_SELECT} ORDER BY d.updated_at DESC, d.id DESC"
        ).fetchall()
    )


def count(keyword: str = "") -> int:
    """满足名称筛选的资料条数（分页要它算总页数）。"""
    where, params = _name_filter(keyword)
    row = get_connection().execute(
        f"SELECT count(*) AS n FROM documents d {where}", params
    ).fetchone()
    return int(row["n"])


def page(keyword: str = "", *, size: int, offset: int) -> list[object]:
    """一页资料（新的在前）。``size`` / ``offset`` 由调用方按页码算好。"""
    where, params = _name_filter(keyword)
    return list(
        get_connection().execute(
            f"{_LIST_SELECT} {where} ORDER BY d.updated_at DESC, d.id DESC LIMIT ? OFFSET ?",
            (*params, int(size), int(offset)),
        ).fetchall()
    )


def _name_filter(keyword: str) -> tuple[str, tuple[object, ...]]:
    """名称筛选的 WHERE 片段（空关键字就是不过滤）。"""
    cleaned = keyword.strip()
    if not cleaned:
        return "", ()
    return "WHERE d.source_name LIKE ?", (f"%{cleaned}%",)
