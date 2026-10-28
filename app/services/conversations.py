"""会话与消息的界面侧服务（UI-free）：落 `conversations` / `messages` 表。"""

from __future__ import annotations

import json
import time
from typing import Any

from app.core.db.connection import get_connection, transaction

DEFAULT_TITLE = "新对话"

#: 手动改名时标题最多几个字（自动命名上限 18，手动可以更长）
RENAME_MAX_CHARS = 40


def ensure_conversation(conversation_id: str, *, title: str = DEFAULT_TITLE) -> None:
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
    """读压缩摘要投影头；没有就返回空摘要 + 0。"""
    row = get_connection().execute(
        "SELECT until_message_id, summary FROM conversation_summaries WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return {"summary": "", "until_message_id": 0}
    return {"summary": str(row["summary"] or ""), "until_message_id": int(row["until_message_id"] or 0)}


def save_summary(conversation_id: str, until_message_id: int, summary: str) -> None:
    """滚动式 upsert：始终只存每会话一条（新摘要替换旧摘要，旧内容被并入）。"""
    with transaction() as conn:
        conn.execute(
            "INSERT INTO conversation_summaries(conversation_id, until_message_id, summary, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(conversation_id) DO UPDATE SET"
            " until_message_id = excluded.until_message_id,"
            " summary = excluded.summary, created_at = excluded.created_at",
            (conversation_id, int(until_message_id), summary, time.time()),
        )


def delete_conversation(conversation_id: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


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
