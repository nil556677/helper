"""T1 统一记忆管理：manager 去重 / PII 脱敏 / 过期归档 / 蒸馏写回 / 召回。"""

from __future__ import annotations  # 延迟值注解

import json  # 序列化
import time  # 时间戳

from app.agent.memory import manager  # 被测
from app.agent.memory.longterm import MemoryFact, MemoryStore  # 底层
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect, get_connection  # 连接


def _store() -> MemoryStore:  # 隔离库 + store
    connect()  # 建空库（get_connection 复用）
    schema_v1.initialize(get_connection())  # 建表（含 TTL 列）
    return MemoryStore()  # store


def test_record_facts_dedup(fake_root) -> None:  # 同 key 同值去重
    _store()  # 建表
    f1 = MemoryFact(key="amount", value=100, source="user_explicit")  # 第一条
    n1 = manager.record_facts([f1], redact_pii=False)  # 写
    n2 = manager.record_facts([f1], redact_pii=False)  # 重复写
    assert n1 == 1  # 首写 1 条
    assert n2 == 0  # 重复跳过
    assert manager.recall("").facts  # 能召回


def test_record_facts_redacts_pii(fake_root) -> None:  # PII 脱敏落库
    _store()  # 建表
    f = MemoryFact(key="contact", value="手机 13812345678", source="paraphrase")  # 含手机号
    written = manager.record_facts([f], redact_pii=True, conversation_id="c1")  # 写（脱敏）
    assert written == 1  # 写入
    row = get_connection().execute(  # 查落库值
        "SELECT value FROM user_memories WHERE memory_key = 'contact' AND status = 'active'"
    ).fetchone()
    assert row  # 有行
    stored = json.loads(row["value"])  # 解析
    assert "13812345678" not in str(stored)  # 明文手机号不在
    assert "REDACTED" in str(stored)  # 已脱敏标记


def test_recall_via_manager_from_store(fake_root) -> None:  # manager 召回走统一入口
    store = _store()  # store
    store.put("pref::lang", "python", source="user_explicit", embedding=[1.0, 0.0], time_s=time.time())  # 写
    bundle = manager.recall("python", top_k=5)  # 召回
    keys = [f["key"] for f in bundle.facts]  # 键
    assert "pref::lang" in keys  # 命中
    assert bundle.profile_lines == []  # P1 画像恒空


def test_forget_expired_archives(fake_root) -> None:  # 过期归档（非物理删）
    store = _store()  # store
    now = time.time()  # 当前
    store.put("expired_fact", 1, source="paraphrase", time_s=now - 1000)  # 写入（无 expires_at，永不过期）
    get_connection().execute(  # 手动设过期时间（在过去）
        "UPDATE user_memories SET expires_at = ? WHERE memory_key = 'expired_fact'",
        (now - 1,),
    )  # 更新
    store.put("stale", 2, source="paraphrase", time_s=now - 1000)  # 另一条
    get_connection().execute(  # 设过期（也在过去）
        "UPDATE user_memories SET expires_at = ? WHERE memory_key = 'stale'",
        (now - 1,),
    )  # 更新
    store.put("fresh", 3, source="paraphrase", time_s=now)  # 未过期
    get_connection().execute(  # 设未来过期
        "UPDATE user_memories SET expires_at = ? WHERE memory_key = 'fresh'",
        (now + 1000,),
    )  # 更新
    get_connection().commit()  # 提交
    n = manager.forget_expired(now=now)  # 归档过期
    assert n >= 2  # 两条过期归档
    res = manager.recall("", top_k=10)  # 召回（排除过期/归档）
    keys = [f["key"] for f in res.facts]  # 键
    assert "fresh" in keys  # 未过期仍在
    assert "expired_fact" not in keys  # 过期不出


def test_distill_conversation_writes(fake_root, monkeypatch) -> None:  # 蒸馏写回（经统一入口）
    _store()  # 建表
    # 禁掉 LLM 画像通道（测试不发真实网络请求），只验证规则通道
    monkeypatch.setattr("app.agent.memory.llm_profile.extract_profile", lambda *a, **k: [])  # 顶掉模型抽取
    from app.services import conversations  # 会话服务（用其建表辅助，保证外键/必需列完整）

    conversations.append_message("c1", role="user", content="帮我看订单 37979", turn_json={"answer": ""})  # 建会话+带订单实体的消息
    messages = conversations.load_messages("c1")  # 取消息（拿自增 id）
    mid = int(messages[0]["id"]) if messages else 1  # 消息 id
    conversations.append_segment(  # 建压缩段引用该消息
        "c1", start_id=mid, end_id=mid, summary="摘要", source_refs=[mid]
    )  # 追加
    n = manager.distill_conversation("c1")  # 蒸馏
    assert n >= 1  # 抽出至少 1 条（订单实体）
    res = manager.recall("订单", top_k=10)  # 召回
    keys = {f["key"] for f in res.facts}  # 键
    assert "order_id" in keys  # 订单实体入库


def test_conflict_stats_exposed(fake_root) -> None:  # 冲突统计可读
    _store()  # 建表
    s = manager.conflict_stats()  # 读
    assert "contested" in s and "superseded" in s  # 两计数都在
    assert manager.conflict_stats() == manager.conflict_stats()  # 幂等读


def test_check_consistency_trims_versions(fake_root) -> None:  # 版本历史超层裁剪
    store = _store()  # store
    store.put("amount", 100, source="paraphrase")  # 底事
    # 手工塞 12 层版本历史
    get_connection().execute(  # 更新
        "UPDATE user_memories SET history_versions = ? WHERE memory_key = 'amount'",
        (json.dumps([{"value": i} for i in range(12)]),),
    )  # 更新
    get_connection().commit()  # 提交
    counts = manager.check_consistency()  # 校验
    assert counts["version_trimmed"] == 1  # 裁剪了一次
    row = get_connection().execute(  # 查层数
        "SELECT history_versions FROM user_memories WHERE memory_key = 'amount'"
    ).fetchone()
    assert len(json.loads(row["history_versions"])) == 10  # 只剩 10 层


def test_check_consistency_archives_stale_contested(fake_root) -> None:  # 悬置冲突归档
    store = _store()  # store
    store.put("amount", 100, source="verified", confidence=0.85, time_s=100)  # 强旧
    store.put("amount", 200, source="paraphrase", confidence=0.3, time_s=300)  # 弱新 → contest
    get_connection().execute(  # 把冲突时序改到 8 天前
        "UPDATE user_memories SET updated_at = ? WHERE status = 'contested'",
        (time.time() - 8 * 86400,),
    )  # 更新
    get_connection().commit()  # 提交
    counts = manager.check_consistency()  # 校验
    assert counts["stale_contested"] == 1  # 悬置冲突归档
    row = get_connection().execute(  # 查状态
        "SELECT status FROM user_memories WHERE status = 'archived' LIMIT 1"
    ).fetchone()
    assert row and row["status"] == "archived"  # 已归档


def test_check_consistency_demotes_overlap_fact(fake_root) -> None:  # 画像冲突事实降权
    _store()  # 建表
    # 画像有 verbosity（白名单键），同时长期事实表存在同键的旧行（直接插模拟历史遗留）
    from app.agent.memory.manager import confirm_profile  # 画像写口

    confirm_profile("verbosity", "short")  # 写画像（进 user_profile）
    get_connection().execute(  # 直接插一条同键事实（模拟未走 manager 的历史遗留）
        "INSERT INTO user_memories(namespace, memory_key, value, source, created_at, updated_at, confidence)"
        " VALUES ('[\"default\", \"user\"]', 'verbosity', '\"long\"', 'paraphrase', ?, ?, 0.8)",
        (time.time(), time.time()),
    )  # 插入
    get_connection().commit()  # 提交
    before = get_connection().execute(  # 事实原置信
        "SELECT confidence FROM user_memories WHERE memory_key = 'verbosity' AND status = 'active'"
    ).fetchone()
    counts = manager.check_consistency()  # 校验
    assert counts["fact_demoted"] == 1  # 降权一次
    after = get_connection().execute(  # 降权后
        "SELECT confidence FROM user_memories WHERE memory_key = 'verbosity' AND status = 'active'"
    ).fetchone()
    assert float(after["confidence"]) < float(before["confidence"])  # 已降权


def test_resolve_conflict_keep_new(fake_root) -> None:  # 冲突复核：采用新值覆盖
    _store()  # 建表
    store = MemoryStore()  # 仓储（同一隔离库）
    store.put("amount", 100, source="verified", confidence=0.85, time_s=100)  # 强旧 active
    store.put("amount", 200, source="paraphrase", confidence=0.3, time_s=300)  # 弱新 → contest
    from app.services import memory as memory_service  # 服务门面

    memory_service.resolve_conflict("amount", keep_new=True)  # 采用新值
    got = store.get("amount")  # 取 active
    assert got["value"] == 200  # 新值生效


def test_resolve_conflict_archive(fake_root) -> None:  # 冲突复核：归档忽略
    _store()  # 建表
    store = MemoryStore()  # 仓储（同一隔离库）
    store.put("amount", 100, source="verified", confidence=0.85, time_s=100)  # 强旧 active
    store.put("amount", 200, source="paraphrase", confidence=0.3, time_s=300)  # 弱新 → contest
    from app.services import memory as memory_service  # 服务门面

    memory_service.resolve_conflict("amount", keep_new=False)  # 归档忽略
    got = store.get("amount")  # 取 active
    assert got["value"] == 100  # 旧值保持
