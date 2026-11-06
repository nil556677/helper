"""L6 审计：运行决策链 append-only（检查点存状态，审计存决策链）。

只提供 append / read；**不提供 update/delete**，保证不可变。
"""

from __future__ import annotations  # 延迟求值注解

import hashlib  # 动作哈希
import json  # 载荷序列化
import sqlite3  # 连接
import time  # 时间戳
from typing import Any  # 任意值

__all__ = ["append_audit", "read_chain"]  # 导出


def _hash(node: str, action: str, payload: dict[str, Any]) -> str:  # 动作哈希
    raw = f"{node}|{action}|{json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)}"  # 规范化
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]  # 短哈希


def append_audit(  # 追加一条决策
    conn: sqlite3.Connection,  # 连接
    run_id: str,  # 运行 id
    node: str,  # 节点
    action: str,  # 动作
    *,
    decision: str = "",  # 决策摘要
    payload: dict[str, Any] | None = None,  # 载荷
) -> str:  # 审计哈希
    """append-only；返回动作哈希（供下游核对）。"""
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_audit WHERE run_id = ?", (run_id,)).fetchone()[0]  # 序号
    digest = _hash(node, action, payload or {})  # 哈希
    conn.execute(  # 插入
        "INSERT INTO run_audit(run_id, seq, node, action, decision, payload, audit_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",  # 语句
        (run_id, seq, node, action, decision, json.dumps(payload or {}, ensure_ascii=False, default=str), digest, time.time()),  # 值
    )
    return digest  # 返回


def read_chain(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:  # 读决策链
    """按 seq 升序返回整条链。"""
    rows = conn.execute(  # 查询
        "SELECT seq, node, action, decision, payload, audit_hash, created_at FROM run_audit WHERE run_id = ? ORDER BY seq",  # 语句
        (run_id,),  # 参数
    ).fetchall()
    return [dict(row) for row in rows]  # 转字典