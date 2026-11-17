"""Skill 审计与候选沉淀表：skill_events(启用/升级/禁用/评分, 只追加) 同步写 SQLite。

对应方案 §7 的"可回退/可留痕"与沉淀流水线的"候选区"。主数据(清单/版本)在 `skills/` 源码，
本表只回答审计与"谁改的、何时启用"。
"""

from __future__ import annotations  # 延迟值注解

import time  # 时间戳
from typing import Any  # 任意值

from app.core.db.connection import get_connection, transaction  # DB

__all__ = ["counts", "record_event"]  # 导出

#: skill_events DDL（只追加）。
_DDL = (  # DDL
    "CREATE TABLE IF NOT EXISTS skill_events ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"  # id
    "  skill_id TEXT NOT NULL,"  # skill id
    "  event_type TEXT NOT NULL,"  # created/enabled/disabled/upgraded/rated/scored
    "  version INTEGER,"  # 版本
    "  actor_id TEXT DEFAULT '' ,"  # 操作者
    "  detail TEXT NOT NULL DEFAULT '{}',"  # 明细（来源 run_id/评分等）
    "  created_at REAL NOT NULL"  # 秒
    ")",
    "CREATE INDEX IF NOT EXISTS idx_skill_events_id_type ON skill_events(skill_id, event_type)",  # 按 skill+类型
)  # 结束


def _ensure() -> None:  # 幂等建表
    with transaction() as conn:  # 单事务
        for stmt in _DDL:  # 逐条
            conn.execute(stmt)  # 执行


def record_event(  # 记一条（同步、只追加）
    *,
    skill_id: str,  # skill id
    event_type: str,  # 事件
    version: int | None = None,  # 版本
    actor_id: str = "",  # 操作者
    detail: dict[str, Any] | None = None,  # 明细
) -> None:  # 无返回
    import json  # JSON

    _ensure()  # 建表
    with transaction() as conn:  # 事务
        conn.execute(  # 插入
            "INSERT INTO skill_events(skill_id, event_type, version, actor_id, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (str(skill_id), str(event_type), int(version) if version is not None else None,
             str(actor_id), json.dumps(detail or {}, ensure_ascii=False), time.time()),  # 参数
        )  # 插入


def counts(skill_id: str = "") -> dict[str, int]:  # 按事件类型聚合
    _ensure()  # 建表
    sql = ("SELECT event_type, COUNT(*) AS c FROM skill_events"  # 聚合
           + (" WHERE skill_id=?" if skill_id else "")  # 过滤
           + " GROUP BY event_type")  # 分组
    params = (skill_id,) if skill_id else ()  # 参数
    rows = get_connection().execute(sql, params).fetchall()  # 行
    return {str(r["event_type"]): int(r["c"]) for r in rows}  # 返回
