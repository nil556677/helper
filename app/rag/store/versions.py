"""版本链：切换 / 收敛候选版本（依赖 ``documents`` 与 Qdrant 的 payload 状态）。"""

from __future__ import annotations

import time

from .chunks import child_keys
from .documents import get, set_status


def switch_version(conn: object, old_doc_id: int, new_doc_id: int, *, now: float | None = None) -> None:
    """**原子切换版本**：旧版本退场、新版本上位。

    **顺序与原子性**（这里是踩过的坑，别改回去）：

    1. 校验两个 id 属于同一 ``doc_key``（不是同一份资料的新旧版本就拒绝）；
    2. **一次** ``set_payloads`` 同时打两组补丁（新→active、旧→superseded）——
       分两次调用会在中间留下"旧已退场、新还没上岗"的窗口，其检索**两版都看不见**
       （检索只认 ``status=active``）；进程若在两次之间被杀，库会长期停在这个状态。
    3. 最后落 DB 状态。Qdrant 成功而 DB 提交失败时最坏只是"短暂新旧都可见"，
       下一轮会自愈；反过来（先 DB 后向量）会留下"数据库说已就位、检索却一条看不见"。
    """
    stamp = time.time() if now is None else now
    keys = {
        str(row["doc_key"] or "")
        for row in conn.execute(  # type: ignore[attr-defined]
            "SELECT doc_key FROM documents WHERE id IN (?, ?)", (old_doc_id, new_doc_id)
        ).fetchall()
    }
    if len(keys) != 1 or "" in keys:
        raise ValueError(
            f"这两个不是同一份资料的版本（doc_key={sorted(keys)}），拒绝切换："
            f"{old_doc_id} → {new_doc_id}"
        )

    from app.core.vector import api

    old_row = get(int(old_doc_id))
    new_row = get(int(new_doc_id))
    old_version = int(old_row["version"] or 1) if old_row is not None else 1  # type: ignore[index]
    new_version = int(new_row["version"] or 1) if new_row is not None else 1  # type: ignore[index]
    api.set_payloads(
        api.COLLECTION_CHUNKS,
        [
            (child_keys(int(new_doc_id), new_version), {"status": "active"}),
            (child_keys(int(old_doc_id), old_version), {"status": "superseded"}),
        ],
    )

    set_status(conn, old_doc_id, "superseded", now=stamp)
    set_status(conn, new_doc_id, "ready", detail=None, now=stamp)


def supersede_stale_candidates(
    conn: object, doc_key: str, *, before_version: int, now: float | None = None
) -> int:
    """把同 doc_key 下**更旧的候选版本**（pending/indexing）退场（P1 审查 H2）。

    为什么需要：候选版本被更高版本"超车"之后就**永远不会被服务**（embed 只做最新版），
    也没有任何路径清理它——它一直占着覆盖率分母和存储。切换时顺手收敛状态机：
    新版本上位的那一刻，所有更旧的候选一起作废（cleanup 随后物理收走）。
    """
    stamp = time.time() if now is None else now
    cursor = conn.execute(  # type: ignore[attr-defined]
        "UPDATE documents SET status = 'superseded', superseded_at = ?, updated_at = ?"
        " WHERE doc_key = ? AND version < ? AND status IN ('pending', 'indexing')",
        (stamp, stamp, doc_key, int(before_version)),
    )
    return int(cursor.rowcount or 0)
