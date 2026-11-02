"""运行状态仓储（异步）：查一轮的状态/结果。

数据来自 ``trace_runs``（driver 收尾写入）；这里以 aiosqlite 异步读取。
"""
from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from app.http.db import AsyncDB  # 异步连接


class RunRepository:  # 运行状态仓储
    def __init__(self, db: AsyncDB) -> None:  # 构造
        self._db = db  # 异步连接

    async def status(self, run_id: str) -> dict[str, Any] | None:  # 状态/结果
        """读一轮的状态/答案/用量；不存在返回 None。"""
        return await self._db.fetch_one(  # 查询
            "SELECT id, status, question, answer, model, started_at, finished_at,"
            " duration_ms, ttft_ms, prompt_tokens, completion_tokens, total_tokens, error_kind"
            " FROM trace_runs WHERE id = ?",
            (run_id,),
        )  # 结束
