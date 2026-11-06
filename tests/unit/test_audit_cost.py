"""L6 审计与成本台账。"""

from __future__ import annotations  # 延迟求值注解

import sqlite3  # 连接

from app.agent.audit import append_audit, read_chain  # 审计
from app.agent.cost import read_cost, record_cost, within_budget  # 成本
from app.core.db import schema_v1  # 建表


def _conn() -> sqlite3.Connection:  # 内存库
    conn = sqlite3.connect(":memory:")  # 连接
    schema_v1.initialize(conn)  # 建表
    return conn  # 返回


def test_audit_append_only_chain() -> None:  # 追加决策链
    conn = _conn()  # 库
    first = append_audit(conn, "r1", "triage", "decide", decision="fast_single", payload={"q": "x"})  # 第一条
    append_audit(conn, "r1", "dispatch", "answer", payload={"tool": "time"})  # 第二条
    chain = read_chain(conn, "r1")  # 读链
    assert [row["seq"] for row in chain] == [1, 2]  # 顺序
    assert chain[0]["audit_hash"] == first and len(first) == 32  # 哈希稳定
    assert chain[1]["node"] == "dispatch"  # 节点
    conn.close()  # 关


def test_cost_ledger_and_budget() -> None:  # 成本与预算
    conn = _conn()  # 库
    record_cost(conn, "r1", model="m", tokens_in=10, tokens_out=20, cost=0.1)  # 记一笔
    usage = read_cost(conn, "r1")  # 聚合
    assert usage == {"tokens_in": 10.0, "tokens_out": 20.0, "cost": 0.1, "calls": 1.0}  # 值
    assert within_budget(conn, "r1", max_tokens=100) is True  # 预算内
    assert within_budget(conn, "r1", max_tokens=10) is False  # 超 token
    assert within_budget(conn, "r1", max_cost=0.01) is False  # 超成本
    conn.close()  # 关