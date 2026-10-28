"""任务看板：只读投影（`trace_runs` 里 kind=agent/task 的行），不新增写表。"""

from __future__ import annotations  # 类型注解延迟求值

from app.core.db.connection import get_connection  # 连接

__all__ = ["task_board"]  # 对外导出

_COLUMNS = (  # 看板需要的列
    "id, parent_run_id, conversation_id, kind, status, question,"
    " started_at, finished_at, duration_ms, error_kind, metadata_json"
)  # 列结束


def task_board(limit: int = 50) -> list[dict]:  # 任务列表
    """按开始时间倒序返回委派任务（只读，不落新表）。"""
    rows = get_connection().execute(  # 查询
        f"SELECT {_COLUMNS} FROM trace_runs"  # 列
        " WHERE kind IN ('agent', 'task')"  # 委派任务
        " ORDER BY started_at DESC LIMIT ?",  # 倒序
        (int(limit),),  # 条数
    ).fetchall()  # 行
    return [dict(row) for row in rows]  # 转字典
