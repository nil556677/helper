"""语义层：启动预计算存储增量 + 三态判定 + 节点写状态。"""

from __future__ import annotations  # 延迟求值注解

import sqlite3  # 直接检查存储
from pathlib import Path  # 临时库

from app.agent.contracts.routing import RouteSpec  # 路径契约
from app.agent.nodes.semantic import node as semantic_mod  # 节点模块
from app.agent.routing.edges import after_rule, after_semantic  # 边函数
from app.agent.routing.embeddings import load_or_build, text_hash  # 索引


def _embed(text: str) -> list[float]:  # 假 embed：按关键词给方向
    if "时间" in text or "几点" in text:  # 时间类
        return [1.0, 0.0]
    if "天气" in text:  # 天气类
        return [0.0, 1.0]
    return [0.0, 0.0]  # 无关


_ROUTES = (  # 两条路径
    RouteSpec(route_id="time", description="查询当前时间"),  # 时间
    RouteSpec(route_id="weather", description="查询天气"),  # 天气
)


def _build(tmp_path: Path, routes=_ROUTES):  # 建索引
    return load_or_build(routes, _embed, store_path=tmp_path / "r.sqlite", model_version="t:2")


def test_store_incremental(tmp_path: Path) -> None:  # 增量：第二次不再 embed
    calls: list[str] = []  # 记录嵌入次数

    def counting(text: str) -> list[float]:  # 计数 versions
        calls.append(text)  # 记录
        return _embed(text)  # 复用

    load_or_build(_ROUTES, counting, store_path=tmp_path / "r.sqlite", model_version="t:2")  # 首次
    first = len(calls)  # 首次次数
    assert first == 2  # 两条路径各一次
    load_or_build(_ROUTES, counting, store_path=tmp_path / "r.sqlite", model_version="t:2")  # 二次
    assert len(calls) == first  # 没新增（全命中）

    rows = sqlite3.connect(str(tmp_path / "r.sqlite")).execute(  # 查表
        "SELECT route_id, text_hash FROM route_vectors ORDER BY route_id"
    ).fetchall()
    assert [row[0] for row in rows] == ["time", "weather"]  # 两行
    assert rows[0][1] == text_hash("查询当前时间")  # 哈希正确


def test_decide_three_states(tmp_path: Path) -> None:  # 三态
    index = _build(tmp_path)  # 索引
    direct = semantic_mod.decide(index, [1.0, 0.0])  # 时间
    assert direct.decision == "direct" and direct.candidates[0].route == "time"  # 直接
    miss = semantic_mod.decide(index, [0.0, 0.0])  # 无关
    assert miss.decision == "miss"  # 未命中


def test_decide_ambiguous_when_close(tmp_path: Path) -> None:  # 近乎同分 → ambiguous
    routes = _ROUTES + (RouteSpec(route_id="time_copy", description="查询当前时间"),)  # 同向量
    index = _build(tmp_path, routes)  # 索引
    recall = semantic_mod.decide(index, [1.0, 0.0])  # 时间
    assert recall.decision == "ambiguous"  # 分不出胜负
    assert recall.margin == 0.0  # 差距为 0


def test_semantic_node_writes_state(tmp_path: Path, monkeypatch) -> None:  # 节点写状态
    index = _build(tmp_path)  # 索引
    monkeypatch.setattr(semantic_mod, "default_embed", lambda: _embed)  # 假 embed
    monkeypatch.setattr(semantic_mod, "default_route_index", lambda: index)  # 假索引
    out = semantic_mod.semantic_node({"question": "现在几点"})  # 跑节点
    assert out["semantic_recall"]["decision"] == "direct"  # 直接
    assert out["route"]["intent"] == "direct"  # 写了统一决策
    assert out["route"]["route"] == "time"  # 路径


def test_edges() -> None:  # 边函数
    assert after_rule({"route": {"intent": "direct"}}) == "dispatch_fixed"  # 命中
    assert after_rule({}) == "semantic_router"  # 未定
    assert after_semantic({"semantic_recall": {"decision": "direct"}}) == "dispatch_fixed"  # 直接
    assert after_semantic({"semantic_recall": {"decision": "ambiguous"}}) == "llm_fallback"  # 兜底
    assert after_semantic({"semantic_recall": {"decision": "miss"}}) == "clarify_or_end"  # 澄清