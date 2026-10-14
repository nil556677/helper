"""``chunks`` 的**唯一写入口**。

为什么必须唯一：FTS5 索引的是 ``chunks.tokens``，而正文在 ``chunks.text``；这两列一旦不同步，
就会出现"看得见正文、搜不到内容"——而且**不会报错**，最难查。所以插入只走这里：
索引文本在函数内部算，调用方**没有机会漏**。
"""

from __future__ import annotations

import time
from collections.abc import Sequence

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
