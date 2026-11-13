"""消息仓储（异步）：查询会话历史消息，以及发送即落库的 user 消息写入。

数据由 agent 的 ``persist_turn`` 写入 ``messages`` 表（channel='chat'）；
本仓储以 aiosqlite 读写，与同步写入靠 WAL 并存。
"""
from __future__ import annotations  # 类型注解延迟求值

import time  # 时间戳
from typing import Any  # 任意值

from app.http.db import AsyncDB  # 异步连接

#: 单页最大条数。
MAX_LIMIT = 100

#: 会话默认标题（与 app.services.conversations 一致）。
DEFAULT_TITLE = "新对话"


def _to_message(row: dict[str, Any]) -> dict[str, Any]:  # 行转 DTO
    """只暴露稳定字段；turn_json 原样给前端（含 process/fragments）。"""
    return {  # 返回
        "id": int(row["id"]),  # 主键
        "role": str(row["role"]),  # user/assistant
        "content": str(row["content"] or ""),  # 文本
        "turn_id": str(row["turn_id"] or "") if row.get("turn_id") else "",  # 轮次 id
        "turn_json": row.get("turn_json"),  # 结构化 Turn（assistant）
        "created_at": float(row.get("created_at") or 0),  # 时间戳
    }  # 结束


class MessageRepository:  # 消息仓储
    def __init__(self, db: AsyncDB) -> None:  # 构造
        self._db = db  # 异步连接

    async def list(  # 历史分页
        self, conversation_id: str, *, before: int | None = None, limit: int = 50
    ) -> dict[str, Any]:  # 分页结果
        """按 seq 倒序取一页 chat 消息；``next_cursor`` = 本页最旧一条 id。"""
        caps = min(max(1, int(limit)), MAX_LIMIT)  # 限幅
        sql = (  # 基础查询
            "SELECT id, seq, role, turn_id, content, turn_json, created_at FROM messages"
            " WHERE conversation_id = ? AND channel = 'chat'"
        )  # 结束
        args: list[Any] = [conversation_id]  # 参数
        if before is not None:  # 有游标
            sql += " AND id < ?"  # 取更旧
            args.append(int(before))  # 追加
        sql += " ORDER BY seq DESC LIMIT ?"  # 最新在前
        args.append(caps)  # 条数
        rows = await self._db.fetch_all(sql, tuple(args))  # 查询
        rows = rows[::-1]  # 回溯为时间正序
        items = [_to_message(r) for r in rows]  # 转换
        next_cursor = str(items[0]["id"]) if items else None  # 本页最旧 id
        return {"items": items, "next_cursor": next_cursor}  # 返回

    async def get(self, conversation_id: str, message_id: int) -> dict[str, Any] | None:  # 单条
        row = await self._db.fetch_one(  # 查
            "SELECT id, seq, role, turn_id, content, turn_json, created_at FROM messages"
            " WHERE conversation_id = ? AND id = ?",
            (conversation_id, int(message_id)),
        )  # 查询
        return _to_message(row) if row is not None else None  # 返回或 None

    async def insert_user(  # 发送即落库：写 user 消息（进行中）
        self, conversation_id: str, question: str, run_id: str
    ) -> int:  # user 消息 id
        """POST 发送时以异步事务写一条 user 消息（status=running，带 run_id）。

        之后 finalize 追加 assistant 时按 run_id 找到它、只补 assistant，不重复插 user。
        """
        stamp = time.time()  # 时间戳
        async with self._db.write_lock:  # 写锁（SQLite 单写者）
            conn = self._db.conn  # 连接
            try:  # 事务
                await conn.execute("BEGIN IMMEDIATE")  # 拿写锁
                await conn.execute(  # 保证会话存在
                    "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (conversation_id, DEFAULT_TITLE, stamp, stamp),
                )  # 结束
                cur = await conn.execute(  # 算 seq
                    "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                )  # 查询
                row = await cur.fetchone()  # 取
                seq = int(row["seq"])  # 序号
                await cur.close()  # 关
                cur = await conn.execute(  # 插 user 消息
                    "INSERT INTO messages(conversation_id, seq, role, content, turn_id, run_id, status, created_at)"
                    " VALUES (?, ?, 'user', ?, ?, ?, 'running', ?)",
                    (conversation_id, seq, question, None, run_id, stamp),
                )  # 插入
                message_id = int(cur.lastrowid)  # 消息 id
                await cur.close()  # 关
                await conn.execute(  # 回填 turn_id（自引用 = 本消息 id）
                    "UPDATE messages SET turn_id = ? WHERE id = ?",
                    (str(message_id), message_id),
                )  # 更新
                await conn.execute(  # 会话时间/标题更新
                    "UPDATE conversations SET updated_at = ?,"
                    " title = CASE WHEN title = ? THEN ? ELSE title END WHERE id = ?",
                    (stamp, DEFAULT_TITLE, question[:18] or DEFAULT_TITLE, conversation_id),
                )  # 更新
                await conn.commit()  # 提交
            except BaseException:  # 失败
                await conn.rollback()  # 回滚
                raise  # 重抛
        return message_id  # 返回消息 id

    async def delete(self, conversation_id: str, message_id: int) -> None:  # 删一条消息
        """删除指定消息（**创建失败补偿用**：user 已先落库但本轮没起来时删掉成交补）。"""
        async with self._db.write_lock:  # 写锁（SQLite 单写者）
            conn = self._db.conn  # 连接
            try:  # 事务
                await conn.execute("BEGIN IMMEDIATE")  # 拿写锁
                await conn.execute(  # 按消息+会话删（限定会话，防误删）
                    "DELETE FROM messages WHERE id = ? AND conversation_id = ?",  # 条件
                    (int(message_id), conversation_id),  # 参数
                )  # 删除
                await conn.commit()  # 提交
            except BaseException:  # 失败
                await conn.rollback()  # 回滚
                raise  # 重抛
