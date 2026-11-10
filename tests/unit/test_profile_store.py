"""T2 用户画像层：ProfileStore 合并规则 / 阈值 / confirm / 渲染 / manager 分流。"""

from __future__ import annotations  # 延迟值注解

from app.agent.memory import manager  # 统一入口（分流/画像）
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect, get_connection  # 连接
from app.memory import (
    model,  # 画像模型
    render,  # 渲染
)
from app.memory.store import ProfileStore  # 画像仓储


def _init() -> None:  # 初始化隔离库（含画像表）
    connect()  # 建空库
    schema_v1.initialize(get_connection())  # 建表（含 user_profile）


def _store() -> ProfileStore:  # 画像仓储
    return ProfileStore(model.default_namespace())  # 默认作用域


def test_upsert_inferred_below_threshold_rejected(fake_root) -> None:  # 推断低于阈值不写
    del fake_root  # 隔离库
    _init()  # 建表
    assert _store().upsert(
        model.ProfileEntry(prop_key="verbosity", value="short", prop_kind="inferred", source="inferred", confidence=0.3),
        infer_threshold=0.7,
    ) is False  # 被拒
    assert _store().get_all() == []  # 无条目


def test_upsert_inferred_high_passes(fake_root) -> None:  # 推断达阈值写入
    del fake_root  # 隔离库
    _init()  # 建表
    ok = _store().upsert(
        model.ProfileEntry(prop_key="verbosity", value="short", prop_kind="inferred", source="inferred", confidence=0.85),
        infer_threshold=0.7,
    )  # 写
    assert ok is True  # active
    assert _store().get_all()[0].prop_key == "verbosity"  # 可读


def test_confirm_overrides_inferred(fake_root) -> None:  # 显式更正覆盖推断（版本+1）
    del fake_root  # 隔离库
    _init()  # 建表
    st = _store()  # 仓储
    st.upsert(model.ProfileEntry(prop_key="verbosity", value="short", source="inferred", confidence=0.85), infer_threshold=0.7)  # 推断
    st.confirm("verbosity", "detailed")  # 用户更正
    entries = st.get_all()  # 列 active
    assert len(entries) == 1  # 只一条 active
    assert entries[0].value == "detailed"  # 新值
    assert entries[0].source == "user_correction"  # 来源已升显式


def test_archive_hides_from_get_all(fake_root) -> None:  # 归档不参与读取
    del fake_root  # 隔离库
    _init()  # 建表
    st = _store()  # 仓储
    st.confirm("language", "zh")  # 写
    st.archive("language")  # 归档
    assert st.get_all() == []  # 不再返回


def test_render_profile_block() -> None:  # 渲染画像块
    entries = [model.ProfileEntry(prop_key="verbosity", value="short", source="user_correction", confidence=0.95)]  # 一条
    text = render.render_profile_block(entries)  # 渲染
    assert "verbosity" in text  # 键在
    assert "short" in text  # 值在
    assert render.render_profile_block([]) == ""  # 空返回空串


def test_manager_recall_includes_profile(fake_root) -> None:  # manager 召回带画像
    _init()  # 建表
    ProfileStore(model.default_namespace()).confirm("verbosity", "short")  # 写画像
    bundle = manager.recall("回答简短点")  # 召回
    assert bundle.profile_lines  # 有画像行
    assert "short" in "\n".join(bundle.profile_lines)  # 值在


def test_record_facts_routes_profile_key(fake_root) -> None:  # 白名单键分流到画像层
    _init()  # 建表
    from app.agent.memory.longterm import MemoryFact  # 事实

    n = manager.record_facts([MemoryFact(key="verbosity", value="short", source="user_explicit")], redact_pii=False)  # 画像键（显式）
    assert n == 1  # 写入
    entries = ProfileStore(model.default_namespace()).get_all()  # 列画像
    assert any(e.prop_key == "verbosity" for e in entries)  # 进画像层


def test_record_facts_low_conf_inferred_not_written(fake_root, monkeypatch) -> None:  # 弱推断画像不写
    _init()  # 建表
    from app.agent.memory.longterm import MemoryFact  # 事实

    monkeypatch.setattr(manager, "_profile_threshold", lambda: 0.7)  # 阈值 0.7
    n = manager.record_facts([MemoryFact(key="verbosity", value="short", source="paraphrase", confidence=0.1)], redact_pii=False)  # 弱来源
    assert n == 0  # 被阈值拦（转述级可信<0.7 且非显式）
    assert ProfileStore(model.default_namespace()).get_all() == []  # 无画像


def test_delete_all_clears_profile(fake_root) -> None:  # 清空含画像
    _init()  # 建表
    ProfileStore(model.default_namespace()).confirm("language", "zh")  # 写画像
    manager.delete_all()  # 清空
    assert ProfileStore(model.default_namespace()).get_all() == []  # 画像已清


def test_vector_module_graceful_fallback(monkeypatch) -> None:  # 向量模块无 Qdrant 时优雅降级
    from app.memory import vector  # 向量模块

    monkeypatch.setattr(vector, "_client", lambda: None)  # 强制"无客户端"（不碰真实 Qdrant）
    assert isinstance(vector.MEMORY_COLLECTION, str)  # 集合名常量
    assert vector.FINGERPRINT_KEY  # 指纹键可在
    # 无客户端时 upsert/delete/search 返回空，不抛异常
    assert vector.upsert_memory_points("n", [{"memory_key": "k", "vector": [1.0] * 8}]) == 0  # 降级
    assert vector.delete_memory_points("n") == 0  # 降级
    assert vector.search_memory_points("n", [1.0] * 8, top_k=3) == []  # 降级空


def test_vector_search_prefers_qdrant(fake_root, monkeypatch) -> None:  # 主力检索：Qdrant 优先于 sqlite
    _init()  # 建表
    import app.agent.memory.longterm as lt  # 底层（patch 开关与查询）
    from app.memory import vector  # 向量模块

    # 模拟 Qdrant 可用且命中（返回 key+score）
    monkeypatch.setattr(lt, "_vector_retrieval_enabled", lambda: True)  # 开向量检索
    monkeypatch.setattr(vector, "ensure_memory_health", lambda: True)  # 假 Qdrant 可用
    monkeypatch.setattr(  # 假 Qdrant 召回
        vector, "search_memory_points",
        lambda ns, qv, top_k=None: [{"memory_key": "pref::lang", "score": 0.95}],  # 命中 lang
    )
    store = lt.MemoryStore()  # 仓储（共享 sqlite）
    store.put("pref::lang", "python", source="user_explicit", confidence=0.9)  # 近事实（有 sqlite 行）
    store.put("pref::city", "成都", source="user_explicit", confidence=0.9)  # 远事实（Qdrant 没命中它）
    ranked = store.search(query_embedding=[1.0, 0.0, 0.0], top_k=5)  # 走 Qdrant → 回补 sqlite
    assert ranked  # 有结果
    assert ranked[0]["key"] == "pref::lang"  # Qdrant 命中的在首位
    assert "pref::city" not in [r["key"] for r in ranked]  # Qdrant 没命中的不返回
    assert ranked[0]["value"] == "python"  # 值从 sqlite 补全
    assert ranked[0]["score"] == 0.95  # score 用 Qdrant 余弦
