"""E5 恢复与对账：扫描/标记/决策/幂等键。"""

from __future__ import annotations  # 类型注解延迟求值

import time  # 时间戳

import pytest  # 测试框架

from app.agent import reconcile  # 被测
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect, get_connection  # 连接


@pytest.fixture(autouse=True)
def _db(fake_root):  # 隔离库
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关


def _seed_run(run_id: str, status: str, age_s: float) -> None:  # 种一行
    get_connection().execute(  # 插
        "INSERT INTO trace_runs(id, question, started_at, status) VALUES (?, ?, ?, ?)",  # 语句
        (run_id, "q", time.time() - age_s, status),  # 参数
    )  # 插


def test_stale_run_ids_finds_only_old_running(fake_root) -> None:  # 扫描
    now = time.time()  # 现在
    _seed_run("old-run", "running", 300)  # 老 running
    _seed_run("fresh-run", "running", 5)  # 新 running
    _seed_run("done-run", "ok", 300)  # 完成的（不算）
    stale = reconcile.stale_run_ids(now=now)  # 扫
    assert stale == ["old-run"]  # 只见老 running


def test_mark_interrupted_only_touches_running(fake_root) -> None:  # 标记
    _seed_run("a", "running", 300)  # running
    _seed_run("b", "ok", 300)  # 已完成
    changed = reconcile.mark_interrupted(["a", "b"])  # 标记
    assert changed == 1  # 只改 a
    row = get_connection().execute(  # 查 a
        "SELECT status FROM trace_runs WHERE id = 'a'",  # 语句
    ).fetchone()  # 行
    assert row["status"] == "interrupted"  # 已标


def test_resume_decision_matrix() -> None:  # 恢复矩阵
    # 只有 interrupt + checkpoint 可 resume
    assert reconcile.resume_decision("running", has_checkpoint=True, waiting_interrupt=True) == "resume"  # interrupt
    assert reconcile.resume_decision("done", has_checkpoint=True) == "new"  # 完成
    assert reconcile.resume_decision("cancelled", has_checkpoint=True) == "new"  # 取消
    assert reconcile.resume_decision("failed", has_checkpoint=True) == "retry"  # 失败 → 有界重试
    assert reconcile.resume_decision(None, has_checkpoint=True) == "interrupted"  # 崩溃 → 标
    assert reconcile.resume_decision("running", has_checkpoint=False) == "interrupted"  # 无 checkpoint 无终态


def test_side_effect_key() -> None:  # 幂等键
    assert reconcile.side_effect_key("t1", 3) == "t1@3"  # 拼接
    assert reconcile.side_effect_key("t1", "write") == "t1@write"  # 字符串 step


def test_reconcile_once_marks_stale_running(fake_root) -> None:  # 运行时对账
    _seed_run("old-r", "running", 300)  # 老 running
    _seed_run("fresh-r", "running", 5)  # 新 running
    changed = reconcile.reconcile_once()  # 一次性对账（默认阈值）
    assert changed == 1  # 只标老的那个
