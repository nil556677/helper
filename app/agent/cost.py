"""L6 成本台账：每次 LLM 调用记 token/成本，按会话聚合，支持经济止损。"""

from __future__ import annotations  # 延迟求值注解

import sqlite3  # 连接
import time  # 时间戳

__all__ = ["read_cost", "record_cost", "within_budget"]  # 导出


def record_cost(  # 记一笔
    conn: sqlite3.Connection,  # 连接
    run_id: str,  # 运行 id
    *,
    model: str = "",  # 模型
    tokens_in: int = 0,  # 输入 token
    tokens_out: int = 0,  # 输出 token
    cost: float = 0.0,  # 估算成本
) -> None:  # 无返回
    """append-only 记账。"""
    conn.execute(  # 插入
        "INSERT INTO cost_ledger(run_id, model, tokens_in, tokens_out, cost, created_at) VALUES (?,?,?,?,?,?)",  # 语句
        (run_id, model, int(tokens_in), int(tokens_out), float(cost), time.time()),  # 值
    )


def read_cost(conn: sqlite3.Connection, run_id: str) -> dict[str, float]:  # 聚合
    """返回 {tokens_in, tokens_out, cost, calls}。"""
    row = conn.execute(  # 聚合
        "SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), COALESCE(SUM(cost),0), COUNT(*) FROM cost_ledger WHERE run_id = ?",  # 语句
        (run_id,),  # 参数
    ).fetchone()
    return {"tokens_in": float(row[0]), "tokens_out": float(row[1]), "cost": float(row[2]), "calls": float(row[3])}  # 返回


def within_budget(  # 经济止损判断
    conn: sqlite3.Connection,  # 连接
    run_id: str,  # 运行 id
    *,
    max_tokens: int = 0,  # 0 = 不限
    max_cost: float = 0.0,  # 0 = 不限
) -> bool:  # 是否在预算内
    """并发地与 recursion_limit 同级；超限由调用方终止并降级。"""
    usage = read_cost(conn, run_id)  # 用量
    if max_tokens and (usage["tokens_in"] + usage["tokens_out"]) > max_tokens:  # 超 token
        return False  # 超限
    if max_cost and usage["cost"] > max_cost:  # 超成本
        return False  # 超限
    return True  # 预算内