"""异步仓储基座：供 agent / HTTP 使用的 async 数据访问（与 sync 版并存）。

A 方案"双适配器"的 AsyncRepo 侧：`ConversationAsyncStore` 提供 agent `load_context`
读历史/摘要所需的异步读取，构造时注入一条 `AsyncDB`（谁开谁关，见 ``app.core.db.aio``）。
同步 `app.services.conversations` 仍服务线程/脚本。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 序列化
import time  # 时间戳
from typing import Any  # 任意值

from app.core.db.aio import AsyncDB  # 异步连接

#: 会话默认标题（与 app.services.conversations 一致）。
_DEFAULT_TITLE = "新对话"


class ConversationAsyncStore:  # 会话异步仓储（读）
    """以 aiosqlite 异步读会话历史/摘要；SQL 与同步版保持一致"""

    def __init__(self, db: AsyncDB) -> None:  # 构造
        self._db = db  # 异步连接

    async def load_messages(self, conversation_id: str) -> list[dict[str, Any]]:  # 历史消息
        """按 seq 顺序取某会话全部 chat 消息（形状与 sync load_messages 一致）。"""
        rows = await self._db.fetch_all(  # 查询
            "SELECT id, seq, role, content, turn_json, created_at FROM messages"
            " WHERE conversation_id = ? ORDER BY seq",
            (conversation_id,),
        )  # 结束
        return rows  # 返回

    async def load_summary(self, conversation_id: str) -> dict[str, Any]:  # 压缩摘要
        """读摘要投影头；没有就返回空摘要 + 0（与 sync load_summary 一致）。"""
        row = await self._db.fetch_one(  # 查询
            "SELECT until_message_id, summary FROM conversation_summaries"
            " WHERE conversation_id = ?",
            (conversation_id,),
        )  # 结束
        if row is None:  # 没有
            return {"summary": "", "until_message_id": 0}  # 空
        return {  # 返回
            "summary": str(row.get("summary") or ""),  # 摘要
            "until_message_id": int(row.get("until_message_id") or 0),  # 覆盖终点
        }  # 结束

    async def pending_user_by_run(  # 找预写 user（发送即落库去重用）
        self, conversation_id: str, run_id: str
    ) -> dict[str, Any] | None:  # 命中行
        """按 run_id 找发送时预写的进行中 user 消息。"""
        return await self._db.fetch_one(  # 查询
            "SELECT id, turn_id FROM messages"
            " WHERE conversation_id = ? AND run_id = ? AND role = 'user'"
            " ORDER BY id LIMIT 1",
            (conversation_id, run_id),
        )  # 结束

    async def append_turn_existing_user(  # 只补 assistant（去重路径）
        self, conversation_id: str, *, turn_id: str, answer: str,
        title: str = _DEFAULT_TITLE, turn_json: dict[str, Any] | None = None,
    ) -> tuple[int, int]:  # (user_id, assistant_id)
        """
        发送即落库升级：user 已预写，只补 assistant 并标 user 完成。单事务。
        """
        stamp = time.time()  # 时间戳
        payload = json.dumps(turn_json, ensure_ascii=False) if turn_json else None  # 序列化
        async with self._db.write_lock:  # 串行写
            conn = self._db.conn  # 连接
            try:  # 事务
                await conn.execute("BEGIN IMMEDIATE")  # 拿写锁
                await conn.execute(  # 保证会话
                    "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)", (conversation_id, title, stamp, stamp),
                )  # 结束
                cur = await conn.execute(  # 算 seq
                    "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                )  # 查询
                row = await cur.fetchone()  # 取
                seq = int(row[0])  # 序号
                await cur.close()  # 关
                cur = await conn.execute(  # 插 assistant（run_id 从预写 user 行带出）
                    "INSERT INTO messages(conversation_id, seq, role, channel, turn_id, content,"
                    " turn_json, run_id, created_at) VALUES (?, ?, 'assistant', 'chat', ?, ?, ?,"
                    " (SELECT run_id FROM messages WHERE id = ?), ?)",
                    (conversation_id, seq, turn_id, answer, payload, int(turn_id), stamp),
                )  # 插入
                assistant_id = int(cur.lastrowid)  # assistant id
                await cur.close()  # 关
                await conn.execute(  # 标 user 完成
                    "UPDATE messages SET status = 'ok' WHERE conversation_id = ? AND role = 'user' AND turn_id = ?",
                    (conversation_id, turn_id),
                )  # 更新
                await conn.execute(  # 会话时间/标题
                    "UPDATE conversations SET updated_at = ?,"
                    " title = CASE WHEN title = ? THEN ? ELSE title END WHERE id = ?",
                    (stamp, _DEFAULT_TITLE, title, conversation_id),
                )  # 更新
                await conn.execute(  # 轮次幂等
                    "INSERT OR IGNORE INTO turn_commits(conversation_id, turn_id, created_at) VALUES (?, ?, ?)",
                    (conversation_id, turn_id, stamp),
                )  # 插入
                await conn.commit()  # 提交
            except BaseException:  # 失败
                await conn.rollback()  # 回滚
                raise  # 重抛
        return int(turn_id), assistant_id  # (user_id, assistant_id)

    async def append_turn(  # 整轮写（非去重路径，无预写 user）
        self, conversation_id: str, *, question: str, answer: str,
        title: str = _DEFAULT_TITLE, turn_json: dict[str, Any] | None = None,
    ) -> tuple[int, int]:  # (user_id, assistant_id)
        """
        无预写 user 时原子写 user+assistant（非 HTTP 路径/回退）。
        """
        stamp = time.time()  # 时间戳
        payload = json.dumps(turn_json, ensure_ascii=False) if turn_json else None  # 序列化
        async with self._db.write_lock:  # 串行写
            conn = self._db.conn  # 连接
            try:  # 事务
                await conn.execute("BEGIN IMMEDIATE")  # 拿写锁
                await conn.execute(  # 保证会话
                    "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)", (conversation_id, title, stamp, stamp),
                )  # 结束
                cur = await conn.execute(  # 算 seq
                    "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                )  # 查询
                row = await cur.fetchone()  # 取
                seq = int(row[0])  # 起始序号
                await cur.close()  # 关
                cur = await conn.execute(  # user 消息
                    "INSERT INTO messages(conversation_id, seq, role, content, run_id, status, created_at)"
                    " VALUES (?, ?, 'user', ?, NULL, 'running', ?)",
                    (conversation_id, seq, question, stamp),
                )  # 插入
                user_id = int(cur.lastrowid)  # user id
                await cur.close()  # 关
                await conn.execute(  # 回填 turn_id（自引用）
                    "UPDATE messages SET turn_id = ? WHERE id = ?", (str(user_id), user_id),
                )  # 更新
                cur = await conn.execute(  # assistant 消息（取 id）
                    "INSERT INTO messages(conversation_id, seq, role, channel, turn_id, content, turn_json, run_id, created_at)"
                    " VALUES (?, ?, 'assistant', 'chat', ?, ?, ?, NULL, ?)",
                    (conversation_id, seq + 1, str(user_id), answer, payload, stamp),
                )  # 插入
                assistant_id = int(cur.lastrowid)  # assistant id
                await cur.close()  # 关
                await conn.execute(  # 会话时间/标题
                    "UPDATE conversations SET updated_at = ?,"
                    " title = CASE WHEN title = ? THEN ? ELSE title END WHERE id = ?",
                    (stamp, _DEFAULT_TITLE, title, conversation_id),
                )  # 更新
                await conn.execute(  # 轮次幂等
                    "INSERT OR IGNORE INTO turn_commits(conversation_id, turn_id, created_at) VALUES (?, ?, ?)",
                    (conversation_id, str(user_id), stamp),
                )  # 插入
                await conn.commit()  # 提交
            except BaseException:  # 失败
                await conn.rollback()  # 回滚
                raise  # 重抛
        return user_id, assistant_id  # (user_id, assistant_id)
