"""T0.1 EAV 键修正与旧数据迁移的回归护栏。"""

from __future__ import annotations  # 延迟值注解

from app.agent.memory import MemoryStore, extract_eav  # 被测
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect, get_connection  # 连接


def _store() -> MemoryStore:  # 隔离库 + store
    connect()  # 建空库（get_connection 复用）
    schema_v1.initialize(get_connection())  # 建表
    return MemoryStore()  # store


def test_reconcile_same_attr_conflicts(fake_root) -> None:  # 同属性不同值 → 软替代/contest 生效
    store = _store()  # store
    store.put("amount", 100, source="user_explicit", confidence=0.98, time_s=100)  # 旧值
    store.put("amount", 120, source="user_explicit", confidence=0.98, time_s=200)  # 新值（更晚）
    got = store.get("amount")  # active
    assert got["value"] == 120  # 新值活跃
    assert len(got["history_versions"]) == 1  # 旧值入历史（软替代）


def test_reconcile_weak_new_contested(fake_root) -> None:  # 弱新 vs 强旧 → contest，旧保持 active
    store = _store()  # store
    store.put("amount", 100, source="verified", confidence=0.85, time_s=100)  # 强旧
    store.put("amount", 200, source="paraphrase", confidence=0.3, time_s=300)  # 弱新
    got = store.get("amount")  # active
    assert got["value"] == 100  # 旧保持 active（fail closed）
    row = get_connection().execute(  # 冲突确认
        "SELECT status FROM user_memories WHERE memory_key = 'amount#contested'"
    ).fetchone()
    assert row and row["status"] == "contested"  # 弱新进 contest


def test_migrate_removes_old_value_in_key(fake_root) -> None:  # 迁移清理旧版"值塞键"
    store = _store()  # store（建表）
    # 预置旧版坏键：属性::值（旧 extract_eav 生成）
    store.put("amount::99.5", 99.5, source="paraphrase")  # 旧坏键
    store.put("order_id::37979", "37979", source="paraphrase")  # 旧坏键
    get_connection().commit()  # 提交
    affected = schema_v1._migrate_user_memories_keys(get_connection())  # 执行迁移
    assert affected >= 2  # 清理了两条
    row = get_connection().execute(  # 坏键应已清除
        "SELECT COUNT(*) AS n FROM user_memories WHERE memory_key LIKE 'amount::%' OR memory_key LIKE 'order_id::%'"
    ).fetchone()
    assert row["n"] == 0  # 无残留


def test_keep_old_memory_archives(fake_root, monkeypatch) -> None:  # 逃生口：归档而非删除
    monkeypatch.setenv("HELPER_KEEP_OLD_MEMORY", "1")  # 开逃生口
    store = _store()  # store（建表）
    store.put("amount::99.5", 99.5, source="paraphrase")  # 旧坏键
    get_connection().commit()  # 提交
    affected = schema_v1._migrate_user_memories_keys(get_connection())  # 执行迁移
    assert affected == 1  # 处理 1 条
    row = get_connection().execute(  # 保留为 archived
        "SELECT status FROM user_memories WHERE memory_key = 'amount::99.5'"
    ).fetchone()
    assert row and row["status"] == "archived"  # 归档非删