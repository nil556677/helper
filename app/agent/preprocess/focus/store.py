"""焦点 · 审计与纠正：focus_events 只追加 + correct_binding（同步写，随轮）。

- 主数据（焦点栈/锚定）在 ChatState（checkpointer）；本表只回答"审计/纠错/跨会话排查"三类问题。
- 同步写（补充方案 §1 建议）：本地 SQLite 便宜 + 纠正读要依赖事件，丢不起。
- 表用幂等 `CREATE TABLE IF NOT EXISTS`（仿 approval 的 `_ensure_*`），既有库也无损。
"""

from __future__ import annotations  # 延迟值注解

import time  # 时间戳
from typing import Any  # 任意值

from app.core.db.connection import get_connection, transaction  # DB

__all__ = ["correct_binding", "counts", "latest_binding", "record_event"]  # 导出

#: focus_events 表 DDL（只追加；禁止 UPDATE/DELETE——纠正也记新事件）。
_DDL = (  # DDL 语句
    "CREATE TABLE IF NOT EXISTS focus_events ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"  # id
    "  thread_id TEXT NOT NULL,"  # 线程
    "  turn_index INTEGER NOT NULL,"  # 轮次
    "  event_type TEXT NOT NULL,"  # entity_added/topic_switched/binding_resolved/binding_ambiguous/binding_corrected
    "  entity_type TEXT,"  # order_id/phone/...
    "  entity_id TEXT,"  # 实体值
    "  group_id TEXT,"  # 有序列表 id
    "  topic TEXT,"  # 话题域
    "  confidence REAL,"  # 置信
    "  detail TEXT NOT NULL DEFAULT '{}',"  # 候选/依据
    "  created_at REAL NOT NULL"  # 秒
    ")",
    "CREATE INDEX IF NOT EXISTS idx_focus_thread_turn ON focus_events(thread_id, turn_index)",  # 线程内按轮
    "CREATE INDEX IF NOT EXISTS idx_focus_events_type ON focus_events(event_type, created_at)",  # 按类型聚合
)  # 结束


def _ensure() -> None:  # 幂等建表
    with transaction() as conn:  # 单事务
        for stmt in _DDL:  # 逐条
            conn.execute(stmt)  # 执行


def record_event(  # 记一条事件（同步、只追加）
    *,
    thread_id: str,  # 线程
    turn_index: int,  # 轮次
    event_type: str,  # 事件类型
    entity_type: str = "",  # 实体类型
    entity_id: str = "",  # 实体值
    group_id: str = "",  # 列表 id
    topic: str = "",  # 话题
    confidence: float = 0.0,  # 置信
    detail: dict[str, Any] | None = None,  # 明细（候选/依据）
) -> None:  # 无返回
    """埋点；调用方已 try/except 兜底，本函数失败不让焦点管理崩。"""
    _ensure()  # 建表（幂等）
    with transaction() as conn:  # 单事务
        conn.execute(  # 插入（只追加）
            "INSERT INTO focus_events(thread_id, turn_index, event_type, entity_type, entity_id,"
            " group_id, topic, confidence, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (  # 参数
                str(thread_id),  # 线程
                int(turn_index),  # 轮
                str(event_type),  # 类型
                str(entity_type),  # 实体类型
                str(entity_id),  # 实体值
                str(group_id),  # 列表
                str(topic),  # 话题
                float(confidence),  # 置信
                _json(detail),  # 明细
                time.time(),  # 秒
            ),  # 结束
        )  # 插入


def latest_binding(  # 取某线程某轮最近一次 binding 事件（纠正用）
    thread_id: str,  # 线程
    turn_index: int,  # 轮次
) -> dict[str, Any] | None:  # 事件或 None
    _ensure()  # 建表
    row = get_connection().execute(  # 查询
        "SELECT * FROM focus_events WHERE thread_id=? AND turn_index=? "
        "AND event_type IN ('binding_resolved','binding_ambiguous') ORDER BY id DESC LIMIT 1",
        (str(thread_id), int(turn_index)),
    ).fetchone()  # 一行
    return dict(row) if row is not None else None  # 返回


def correct_binding(  # 纠正绑定（只追加 + 现场可见）
    thread_id: str,  # 线程
    turn_index: int,  # 轮次
    new_entity_id: str,  # 新绑定
    *,  # 关键字
    new_entity_type: str = "order_id",  # 新实体类型
) -> dict[str, Any]:  # 结果（含原绑定，供调用方 update_state）
    """记一条 binding_corrected（不改原事件），返回纠正上下文（原绑定/候选）。"""
    original = latest_binding(thread_id, turn_index)  # 原绑定事件
    detail = {  # 明细
        "original": (original or {}).get("entity_id") or "",  # 原绑定
        "candidates": _parse(original) if original else [],  # 候选
    }  # 结束
    record_event(  # 记纠正
        thread_id=thread_id,  # 线程
        turn_index=turn_index,  # 轮
        event_type="binding_corrected",  # 类型
        entity_type=new_entity_type,  # 实体类型
        entity_id=new_entity_id,  # 新绑定
        detail=detail,  # 明细
    )  # 结束
    return {"thread_id": thread_id, "turn_index": turn_index, "original": detail["original"], "new_entity_id": new_entity_id}  # 返回


def _parse(row: dict[str, Any]) -> list[Any]:  # 解析 detail 里的候选
    try:  # 解析 JSON
        import json  # JSON

        d = json.loads(row.get("detail") or "{}")  # 明细
        return list(d.get("candidates") or [])  # 候选
    except Exception:  # 坏数据
        return []  # 空


def counts(thread_id: str = "") -> dict[str, int]:  # 按事件类型聚合（跨会话/单线程排查）
    """返回 {event_type: count}；`thread_id` 给定时只看该线程（默认全量）。"""
    _ensure()  # 建表
    sql = ("SELECT event_type, COUNT(*) AS c FROM focus_events"  # 聚合
           + (" WHERE thread_id=?" if thread_id else "")  # 过滤线程
           + " GROUP BY event_type")  # 分组
    params = (thread_id,) if thread_id else ()  # 参数
    rows = get_connection().execute(sql, params).fetchall()  # 行
    return {str(r["event_type"]): int(r["c"]) for r in rows}  # 返回


def _json(detail: dict[str, Any] | None) -> str:  # 明细序列化
    import json  # JSON

    return json.dumps(detail or {}, ensure_ascii=False)  # 字符串
