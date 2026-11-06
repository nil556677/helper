"""L1 预处理：实体/时间/flags/改写 + 节点写入。"""

from __future__ import annotations  # 延迟求值注解

from datetime import datetime  # 固定"今天"

from app.agent.contracts.triage import EntityBundle  # 契约
from app.agent.nodes.preprocess.node import preprocess_node  # 节点
from app.agent.preprocess.entities import extract_entities, inherit_from_turns  # 实体
from app.agent.preprocess.flags import collect_flags  # flags
from app.agent.preprocess.rewrite import maybe_rewrite, needs_rewrite  # 改写
from app.agent.preprocess.timeparse import resolve_time  # 时间


def test_extract_entities() -> None:  # 正则抽取
    found = extract_entities("订单 12345 能不能退，金额 99.5 元")  # 抽
    assert found["order_id"] == "12345"  # 订单号
    assert found["amount"] == "99.5"  # 金额


def test_inherit_from_turns() -> None:  # 历史继承
    turns = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "", "entities": {"order_id": "A1"}}]  # 历史
    assert inherit_from_turns(turns) == {"order_id": "A1"}  # 补上
    assert inherit_from_turns([{"role": "user", "content": "x"}]) == {}  # 无结构化就不猜


def test_resolve_time_relative() -> None:  # 相对时间
    now = datetime(2026, 11, 5)  # 固定
    r = resolve_time("上季度怎么样", now)  # 解析
    assert r is not None and r.start and r.end  # 有范围
    assert r.start < r.end  # 顺序正确
    assert resolve_time("随便说说", now) is None  # 解析不了


def test_resolve_time_single_day() -> None:  # 单日
    now = datetime(2026, 11, 5)  # 固定
    r = resolve_time("昨天发生了什么", now)  # 昨天
    assert r is not None and r.start == "2026-11-04" and r.end == "2026-11-04"  # 单日


def test_flags_whitelist() -> None:  # flags 白名单
    assert collect_flags({"mode": "answer_only", "evil": "x"}) == {"mode": "answer_only"}  # 只留白名单


def test_rewrite_seam() -> None:  # 改写接缝
    entities = EntityBundle(explicit={})  # 无显式实体
    assert needs_rewrite("它多少钱", entities) is True  # 有指代
    assert needs_rewrite("订单 12345 多少钱", EntityBundle(explicit={"order_id": "12345"})) is False  # 有实体
    q, done, reason = maybe_rewrite("它多少钱", entities, {"no_rewrite": True}, None)  # 被禁
    assert (q, done, reason) == ("它多少钱", False, "flag_blocked")  # 不改
    q2, done2, _ = maybe_rewrite("它多少钱", entities, {}, lambda text, ents: "订单 12345 多少钱")  # 有实现
    assert done2 is True and "12345" in q2  # 改写生效


def test_preprocess_node_writes_state() -> None:  # 节点写 preprocessed
    out = preprocess_node({"question": "订单 12345 昨天怎么样", "turns": []})  # 跑
    pre = out["preprocessed"]  # 产物
    assert pre["entities"]["explicit"]["order_id"] == "12345"  # 实体
    assert pre["time_range"]["start"]  # 时间
    assert pre["phase"] == "start"  # 阶段
    assert out["question"] == "订单 12345 昨天怎么样"  # query 透传