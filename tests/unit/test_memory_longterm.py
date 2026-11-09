"""M3 长期可信知识库：置信度/衰减/冲突仲裁/召回/规则抽取。"""

from __future__ import annotations  # 延迟值注解

import time  # 时间戳

from app.agent.memory import (  # 被测
    MemoryFact,  # 事实
    MemoryStore,  # 仓储
    confidence_for,  # 可信度
    decay_score,  # 衰减
    extract_eav,  # 抽取
)
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect, get_connection  # 连接


def _store() -> MemoryStore:  # 隔离库 + store
    connect()  # 建空库（get_connection 复用）
    schema_v1.initialize(get_connection())  # 建表
    return MemoryStore()  # store


def test_confidence_source_table() -> None:  # 来源表 + 合并 + 含糊压限
    high = confidence_for(MemoryFact(key="k", value="以后都用类型注解", source="user_explicit"))  # 显式
    low = confidence_for(MemoryFact(key="k", value="他好像会处理", source="hedged"))  # 含糊
    assert high > low  # 显式更高
    assert low <= 0.5  # 含糊压限
    assert 0.0 <= confidence_for(MemoryFact(key="k", value="x", source="x")) <= 1.0  # 未知来源安全


def test_decay_decreases_with_age_and_no_access() -> None:  # 衰减
    base = 0.9  # 可信度
    fresh = decay_score(base, updated_at=1000, now=1001)  # 刚写
    old = decay_score(base, updated_at=1000, now=1000 + 40 * 86400)  # 40 天后
    accessed = decay_score(base, updated_at=1000, now=1001, access_count=5, last_access=1001)  # 常访问
    assert fresh > old  # 时间衰减
    assert accessed > old  # 访问提权


def test_put_get_and_access_count(fake_root) -> None:  # 写/读/访问记账
    store = _store()  # store
    store.put("orders::37979::status", "已发货", source="verified")  # 写
    got = store.get("orders::37979::status")  # 读
    assert got["value"] == "已发货"  # 值
    assert got["confidence"] > 0.5  # 检索验证级可信
    store.get("orders::37979::status")  # 再访问
    row = get_connection().execute(  # 查 access_count
        "SELECT access_count FROM user_memories WHERE memory_key = 'orders::37979::status'"
    ).fetchone()
    assert row["access_count"] >= 2  # 记账


def test_reconcile_soft_supersede(fake_root) -> None:  # 软替代保留历史
    store = _store()  # store
    store.put("orders::1::amount", 100, source="user_explicit", time_s=100)  # 旧
    store.put("orders::1::amount", 120, source="user_explicit", confidence=0.98, time_s=200)  # 新更可信更晚
    got = store.get("orders::1::amount")  # active
    assert got["value"] == 120  # 新活跃
    assert len(got["history_versions"]) == 1  # 旧版保留
    assert got["history_versions"][0]["value"] == 100  # 可追溯


def test_reconcile_contested_fail_closed(fake_root) -> None:  # 冲突且弱 → contest，旧保持 active
    store = _store()  # store
    store.put("orders::2::status", "已发货", source="verified", confidence=0.85, time_s=100)  # 强旧
    store.put("orders::2::status", "已退款", source="paraphrase", confidence=0.3, time_s=300)  # 弱新
    got = store.get("orders::2::status")  # active
    assert got["value"] == "已发货"  # 旧保持 active（fail closed）
    row = get_connection().execute(  # 冲突记录
        "SELECT status FROM user_memories WHERE memory_key = 'orders::2::status#contested'"
    ).fetchone()
    assert row and row["status"] == "contested"  # 弱新进 contest


def test_search_returns_only_active_by_decay(fake_root) -> None:  # 召回只取 active 按衰减排序
    store = _store()  # store
    store.put("pref::lang", "python", source="user_explicit", time_s=1000)  # 老但常访问
    store.put("pref::city", "成都", source="user_explicit", confidence=0.99, time_s=time.time())  # 新近
    store.put("orders::x::status", "已发货", source="verified", time_s=time.time())  # 活跃
    ranked = store.search(top_k=5)  # 召回
    keys = [r["key"] for r in ranked]  # 键序列
    assert "orders::x::status" in keys  # 活跃在召回
    assert all(0 <= r["score"] <= 1 for r in ranked)  # 分合法


def test_extract_eav_rules() -> None:  # 规则抽 EAV 事实
    facts = extract_eav("订单 37979 金额 99.5 元，昨天处理")  # 一句话
    keys = {f.key for f in facts}  # 键
    assert "order_id::37979" in keys  # 订单
    assert "amount::99.5" in keys  # 金额


def test_cosine_sim() -> None:  # 余弦相似度
    from app.agent.memory import cosine_sim  # 余弦

    assert round(cosine_sim([1, 0], [1, 0]), 4) == 1.0  # 同向
    assert round(cosine_sim([1, 0], [0, 1]), 4) == 0.0  # 垂直
    assert cosine_sim([], [1]) == 0.0  # 坏输入安全


def test_search_with_query_embedding_ranks(fake_root) -> None:  # 语义召回：余弦加分排序
    store = _store()  # store
    store.put("pref::lang", "python", source="user_explicit", embedding=[1.0, 0.0, 0.0], time_s=time.time())  # 与查询近
    store.put("pref::city", "成都", source="user_explicit", embedding=[0.0, 0.0, 1.0], time_s=time.time())  # 与查询远
    ranked = store.search(query_embedding=[1.0, 0.0, 0.0], top_k=2)  # 按余弦召回
    assert ranked[0]["key"] == "pref::lang"  # 近的排前
    assert all(0 <= r["score"] <= 1 for r in ranked)  # 分合法