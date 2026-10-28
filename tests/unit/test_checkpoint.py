"""E2 检查点：SQLite 持久化机制的写入/读取/修剪/配置（用同步 saver + 同步 invoke 验证机制）。"""

from __future__ import annotations  # 类型注解延迟求值

import sqlite3  # 清理统计
from pathlib import Path  # 路径
from typing import TypedDict  # 状态类型

from langgraph.checkpoint.sqlite import SqliteSaver  # 同步 saver（机制验证）
from langgraph.graph import END, START, StateGraph  # 图原语

from app.agent import checkpoint  # 被测


class _S(TypedDict, total=False):  # 小状态
    x: str  # 一个字段


def _node_a(state: _S) -> _S:  # 节点（同步）
    del state  # 不用
    return {"x": "a1"}  # 写


def _graph(saver):  # 编一个小图
    g = StateGraph(_S)  # 图
    g.add_node("a", _node_a)  # 节点
    g.add_edge(START, "a")  # 入口
    g.add_edge("a", END)  # 出口
    return g.compile(checkpointer=saver)  # 编译（挂检查点）


def _cfg(thread: str) -> dict:  # config
    return {"configurable": {"thread_id": thread}}  # 返回


def test_config_for_carries_thread_and_metadata() -> None:  # 配置
    cfg = checkpoint.config_for("c1", turn_id="t1", attempt=3)  # 组装
    assert cfg["configurable"]["thread_id"] == "c1"  # 线程
    assert cfg["metadata"] == {"turn_id": "t1", "attempt": 3}  # 元数据


def test_checkpoint_writes_and_reads_latest(fake_root) -> None:  # 写读
    path = str(Path(fake_root) / "cp.sqlite")  # 库文件
    with SqliteSaver.from_conn_string(path) as saver:  # 同步 saver
        graph = _graph(saver)  # 图
        cfg = _cfg("conv-1")  # 配置
        graph.invoke({}, cfg)  # 同步跑一轮（机制验证）
        st = graph.get_state(cfg)  # 同步读最新
        assert st.next == ()  # 走到 END（无下一步）
        assert st.values == {"x": "a1"}  # 状态留住


def test_checkpoint_prune_keeps_latest_per_thread(fake_root) -> None:  # 清理
    path = str(Path(fake_root) / "cp2.sqlite")  # 库文件
    with SqliteSaver.from_conn_string(path) as saver:  # 同步 saver
        graph = _graph(saver)  # 图
        cfg_a = _cfg("t-a")  # 线程 A
        cfg_b = _cfg("t-b")  # 线程 B
        for _ in range(3):  # A 跑 3 次
            graph.invoke({}, cfg_a)  # 执行
        graph.invoke({}, cfg_b)  # B 跑 1 次

    removed = checkpoint.prune(keep=2, path=path)  # 清理
    assert removed >= 1  # 删了
    for thread in ("t-a", "t-b"):  # 逐线程
        count = _count_checkpoints(path, thread)  # 统计
        assert count <= 2  # 不超过保留数


def test_assembly_override_returns_injected_graph(fake_root) -> None:  # worker 注入图
    import app.agent.assembly as a  # 装配

    a.set_graph("injected")  # 注入
    try:  # 用完复位
        assert a.build() == "injected"  # build 用注入图
    finally:  # 复位
        a.reset()  # 清
        assert a.build() is not None  # 复位后重建真实图


def _count_checkpoints(path: str, thread: str) -> int:  # 统计个数
    conn = sqlite3.connect(path)  # 开
    try:  # 有异常也关
        return int(  # 返回
            conn.execute(  # 查询
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread,)  # 条件
            ).fetchone()[0]  # 首列
        )  # 返回
    finally:  # 关
        conn.close()  # 关


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:  # 表是否存在
    row = conn.execute(  # 查 sqlite_master
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)  # 条件
    ).fetchone()  # 结果
    return row is not None  # 返回


def _count_orphan_writes(path: str) -> int:  # 统计孤儿旁表行
    """指向已不存在 checkpoint 的写入记录（prune 必须一并清掉，否则无限增长）。"""
    conn = sqlite3.connect(path)  # 开
    try:  # 有异常也关
        total = 0  # 累计
        for table in ("writes", "checkpoint_writes"):  # 兼容不同版本表名
            if not _table_exists(conn, table):  # 表不在
                continue  # 跳过
            total += int(conn.execute(  # 累加孤儿行
                f"SELECT COUNT(*) FROM {table} w WHERE NOT EXISTS ("  # 没有对应 checkpoint
                "SELECT 1 FROM checkpoints c WHERE c.thread_id = w.thread_id"  # 同线程
                " AND c.checkpoint_ns = w.checkpoint_ns"  # 同命名空间
                " AND c.checkpoint_id = w.checkpoint_id)",  # 同 id
            ).fetchone()[0])  # 首列
        return total  # 返回
    finally:  # 关
        conn.close()  # 关


def test_prune_removes_orphan_writes(fake_root) -> None:  # 回归：prune 清旁表
    """回归：prune 只删 checkpoints 不删 writes，旁表会无限增长（真实库验证过）。"""
    path = str(Path(fake_root) / "cp3.sqlite")  # 库文件
    with SqliteSaver.from_conn_string(path) as saver:  # 同步 saver
        graph = _graph(saver)  # 图
        cfg = _cfg("t-w")  # 线程
        for _ in range(4):  # 跑 4 轮（每轮至少一条 checkpoint + 写入）
            graph.invoke({}, cfg)  # 执行
        assert _count_orphan_writes(path) == 0  # 前置：完整保留时不应有孤儿
    removed = checkpoint.prune(keep=1, path=path)  # 只留 1 个
    assert removed >= 1  # 确实删了检查点
    assert _count_orphan_writes(path) == 0  # 关键：没有指向已删 checkpoint 的孤儿行
