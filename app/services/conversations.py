"""会话与消息的界面侧服务（UI-free）：落 `conversations` / `messages` 表。"""

from __future__ import annotations

import json
import time
from typing import Any

from app.core.db.connection import get_connection, transaction

DEFAULT_TITLE = "新对话"

#: 手动改名时标题最多几个字（自动命名上限 18，手动可以更长）
RENAME_MAX_CHARS = 40


def tombstoned(conversation_id: str) -> bool:
    """该会话是否已被删除（墓碑）：是则禁止重建/写入，防进行中的 run 复活会话。"""
    row = get_connection().execute(
        "SELECT 1 FROM conversation_tombstones WHERE id = ?", (conversation_id,)
    ).fetchone()
    return row is not None


def ensure_conversation(conversation_id: str, *, title: str = DEFAULT_TITLE) -> None:
    if tombstoned(conversation_id):  # 已删会话：不重建（墓碑优先）
        return
    with transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (conversation_id, title, time.time(), time.time()),
        )


def next_seq(conversation_id: str) -> int:
    row = get_connection().execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    return int(row["seq"])


def append_message(
    conversation_id: str,
    *,
    role: str,
    content: str,
    turn_json: dict[str, Any] | None = None,
    title: str = DEFAULT_TITLE,
) -> None:
    if tombstoned(conversation_id):  # 已删会话：不写（防复活）
        return
    ensure_conversation(conversation_id, title=title)
    stamp = time.time()
    with transaction() as conn:
        seq = int(
            conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["seq"]
        )
        conn.execute(
            "INSERT INTO messages(conversation_id, seq, role, content, turn_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                seq,
                role,
                content,
                json.dumps(turn_json, ensure_ascii=False) if turn_json else None,
                stamp,
            ),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ?, title = CASE WHEN title = ? THEN ? ELSE title END"
            " WHERE id = ?",
            (stamp, DEFAULT_TITLE, title, conversation_id),
        )


def append_turn(
    conversation_id: str,
    *,
    question: str,
    answer: str,
    title: str = DEFAULT_TITLE,
    turn_json: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """一轮问答**原子写入**：user + assistant 同一个事务，避免留下半轮。

    为什么不用两次 ``append_message``：进程在两条之间退出时，历史里会出现
    “只有用户、没有助手”的孤儿轮，重放时整轮丢失（``restore_from_db`` 按配对读取）。

    返回：``(user_id, assistant_id)``（消息 id，供 trace/对账关联）。
    """
    if title == DEFAULT_TITLE and question:  # 没显式给标题
        title = question[:18]  # 用问题前 18 字（与 append_message 的自动命名一致）
    if tombstoned(conversation_id):  # 已删会话：不写（防止进行中的 run 复活它）
        return (-1, -1)  # 跳过
    ensure_conversation(conversation_id, title=title)  # 先保证会话存在
    stamp = time.time()  # 同一时刻写入
    payload = json.dumps(turn_json, ensure_ascii=False) if turn_json else None  # 序列化一次
    with transaction() as conn:  # 单事务
        seq = int(  # 起始序号
            conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["seq"]
        )
        user_cursor = conn.execute(  # 用户消息
            "INSERT INTO messages(conversation_id, seq, role, content, turn_json, created_at)"
            " VALUES (?, ?, 'user', ?, NULL, ?)",
            (conversation_id, seq, question, stamp),
        )
        turn_id = str(int(user_cursor.lastrowid))  # 逻辑轮次 id = user 消息 id
        conn.execute(  # 给用户行回填 turn_id（自引用）
            "UPDATE messages SET turn_id = ? WHERE id = ?",
            (turn_id, int(user_cursor.lastrowid)),
        )
        assistant_cursor = conn.execute(  # 助手消息（答案为空也写，保证配对）
            "INSERT INTO messages(conversation_id, seq, role, channel, turn_id, content, turn_json, created_at)"
            " VALUES (?, ?, 'assistant', 'chat', ?, ?, ?, ?)",
            (conversation_id, seq + 1, turn_id, answer, payload, stamp),
        )
        conn.execute(  # 会话标题/更新时间
            "UPDATE conversations SET updated_at = ?, title = CASE WHEN title = ? THEN ? ELSE title END"
            " WHERE id = ?",
            (stamp, DEFAULT_TITLE, title, conversation_id),
        )
        conn.execute(  # 轮次幂等标记（同一事务；幂等，已存在则不重复）
            "INSERT OR IGNORE INTO turn_commits(conversation_id, turn_id, created_at) VALUES (?, ?, ?)",
            (conversation_id, turn_id, stamp),
        )
        return int(user_cursor.lastrowid), int(assistant_cursor.lastrowid)  # 两个消息 id


def pending_user_by_run(conversation_id: str, run_id: str) -> dict[str, Any] | None:
    """按 run_id 找发送时预写的「进行中」user 消息（供 finalize 追加 assistant）。"""
    row = get_connection().execute(
        "SELECT id, turn_id FROM messages"
        " WHERE conversation_id = ? AND run_id = ? AND role = 'user'"
        " ORDER BY id LIMIT 1",
        (conversation_id, run_id),
    ).fetchone()
    return dict(row) if row is not None else None


def append_turn_existing_user(
    conversation_id: str,
    *,
    turn_id: str,
    answer: str,
    title: str = DEFAULT_TITLE,
    turn_json: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """发送即落库时：user 已预写，只补 assistant 并把 user 标成完成。

    返回：``(user_id, assistant_id)``。同样原子写（在一个事务里）。
    """
    if tombstoned(conversation_id):  # 已删会话：不补写 assistant（防复活）
        return (-1, -1)  # 跳过
    ensure_conversation(conversation_id, title=title)  # 保证会话存在
    stamp = time.time()  # 时间戳
    payload = json.dumps(turn_json, ensure_ascii=False) if turn_json else None  # 序列化
    with transaction() as conn:  # 单事务
        seq = int(  # 起始序号
            conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["seq"]
        )
        assistant_cursor = conn.execute(  # 助手消息
            "INSERT INTO messages(conversation_id, seq, role, channel, turn_id, content, turn_json, run_id, created_at)"
            " VALUES (?, ?, 'assistant', 'chat', ?, ?, ?,"
            " (SELECT run_id FROM messages WHERE id = ?), ?)",
            (conversation_id, seq, turn_id, answer, payload, int(turn_id), stamp),
        )
        conn.execute(  # 把预写的 user 消息标完成
            "UPDATE messages SET status = 'ok' WHERE conversation_id = ? AND role = 'user' AND turn_id = ?",
            (conversation_id, turn_id),
        )
        conn.execute(  # 会话标题/更新时间
            "UPDATE conversations SET updated_at = ?, title = CASE WHEN title = ? THEN ? ELSE title END"
            " WHERE id = ?",
            (stamp, DEFAULT_TITLE, title, conversation_id),
        )
        conn.execute(  # 轮次幂等标记（同一事务）
            "INSERT OR IGNORE INTO turn_commits(conversation_id, turn_id, created_at) VALUES (?, ?, ?)",
            (conversation_id, turn_id, stamp),
        )
        return int(turn_id), int(assistant_cursor.lastrowid)  # 两个消息 id


def list_conversations() -> list[dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT id, title, created_at, updated_at FROM conversations"
        " ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def load_messages(conversation_id: str) -> list[dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT id, seq, role, content, turn_json, created_at FROM messages"
        " WHERE conversation_id = ? ORDER BY seq",
        (conversation_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_summary(conversation_id: str) -> dict[str, Any]:
    """读最后一个分段摘要作为投影头；没有分段就返回空摘要 + 0。"""
    row = get_connection().execute(
        "SELECT seg_seq, end_id, summary FROM summary_segments"
        " WHERE conversation_id = ? ORDER BY seg_seq DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return {"summary": "", "until_message_id": 0}
    return {"summary": str(row["summary"] or ""), "until_message_id": int(row["end_id"] or 0)}


def load_segments(conversation_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """读分段摘要（新 → 旧，保留最近 limit 段供上游按 token 预算取用）。

    注意：这是**滚动摘要**——最后一段已包含此前全部内容（append 时带旧摘要重算）。
    上下文注入应只取 1 段（最新），避免旧段内容重复进 prompt；UI 历史展示才取多段。
    """
    rows = get_connection().execute(
        "SELECT seg_seq, start_id, end_id, summary, source_refs, created_at FROM summary_segments"
        " WHERE conversation_id = ? ORDER BY seg_seq DESC LIMIT ?",
        (conversation_id, int(limit)),
    ).fetchall()
    return [dict(row) for row in rows]


def segment_count(conversation_id: str) -> int:
    """某会话现有分段数（压满 max_compactions 时封顶用）。"""
    row = get_connection().execute(
        "SELECT COUNT(*) AS n FROM summary_segments WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    return int((row["n"] if row else 0) or 0)


def append_segment(conversation_id: str, *, start_id: int, end_id: int, summary: str, source_refs: list[int]) -> None:
    """追加式写入一段摘要（永不重写旧段；seg_seq 自动递增）。"""
    seg = get_connection().execute(
        "SELECT COALESCE(MAX(seg_seq), 0) + 1 AS n FROM summary_segments WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    seg_seq = int((seg["n"] if seg else 0) or 1)
    import json  # 序列化 refs

    with transaction() as conn:
        conn.execute(
            "INSERT INTO summary_segments(conversation_id, seg_seq, start_id, end_id, summary, source_refs, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, seg_seq, int(start_id), int(end_id), str(summary),
             json.dumps([int(r) for r in source_refs], ensure_ascii=False), time.time()),
        )


def delete_conversation(conversation_id: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        conn.execute(  # 立墓碑：禁止进行中的 run 通过 ensure 重建
            "INSERT OR REPLACE INTO conversation_tombstones(id, deleted_at) VALUES (?, ?)",
            (conversation_id, time.time()),
        )


def rename_conversation(conversation_id: str, title: str) -> str:
    """改会话名；空/全空白回退默认标题，超长截断。返回最终标题。

    自定义标题不会被自动命名覆盖：``append_message`` 只在标题仍等于默认时才自动改名，
    所以这里手动写入后，新消息不会把用户起的名字顶掉。
    """
    cleaned = (title or "").strip()  # 去首尾空白
    if not cleaned:  # 空/全空白 → 回退默认标题（写默认值，别写空串）
        cleaned = DEFAULT_TITLE  # 默认
    cleaned = cleaned[:RENAME_MAX_CHARS]  # 超长截断
    stamp = time.time()  # 时间戳
    with transaction() as conn:
        conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (cleaned, stamp, conversation_id),
        )
    return cleaned  # 最终标题
