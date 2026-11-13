"""P1–P5 焦点管理：实体抽取硬约束 / 焦点栈 / 话题切换 / 指代消解 / focus 节点。"""

from __future__ import annotations  # 延迟值注解

from app.agent.contracts.focus import FocusEntity, FocusStack  # 契约
from app.agent.nodes.preprocess.node import (
    preprocess_node as focus_node,  # 预处理节点（内含焦点管理）
)
from app.agent.preprocess.focus.extract import extract_entities  # 抽取
from app.agent.preprocess.focus.resolve import (  # 消解
    resolve_ordinal,
    resolve_reference,
    resolve_with_chain,
)
from app.agent.preprocess.focus.stack import new_stack, update_focus_stack  # 栈
from app.agent.preprocess.focus.topic import detect_topic_switch  # 话题


def _stack() -> FocusStack:  # 一个带实体的栈
    fs = new_stack()  # 空
    fs.stack = [  # 直接放实体
        FocusEntity(entity_type="order_id", entity_id="12345", surface="12345", topic="order", last_mentioned_at=1, mention_count=1, weight=1.0, confidence=1.0, source="regex"),  # 订单A
        FocusEntity(entity_type="order_id", entity_id="67890", surface="67890", topic="order", last_mentioned_at=2, mention_count=1, weight=1.0, confidence=1.0, source="regex"),  # 订单B
    ]  # 结束
    fs.turn_index = 2  # 当前第 2 轮
    fs.current_topic = "order"  # 话题
    return fs  # 返回


# —— 实体抽取 ——
def test_bare_number_needs_context() -> None:  # 裸数字无上下文不认成订单
    assert not [e for e in extract_entities("这单金额是 12345 元") if e.entity_type == "order_id"]  # 无业务词不认


def test_prefixed_order_high_confidence() -> None:  # 带前缀订单号高置信
    es = extract_entities("查一下订单 12345")  # 抽取
    hits = [e for e in es if e.entity_type == "order_id"]  # 订单
    assert hits  # 有
    assert hits[0].confidence >= 0.8  # 高置信


def test_bare_with_context_regex() -> None:  # 裸数字+业务词 0.8
    es = extract_entities("帮我看看订单号 12345 到哪了")  # 抽取
    hits = [e for e in es if e.entity_type == "order_id"]  # 订单
    assert hits and hits[0].confidence >= 0.7  # 0.8 档


def test_phone_exact_format() -> None:  # 手机号格式命中
    es = extract_entities("手机号 15196630910")  # 抽取
    hits = [e for e in es if e.entity_type == "phone"]  # 电话
    assert hits and hits[0].entity_id == "15196630910"  # 命中


def test_structured_constant_high() -> None:  # 结构化字段恒高置信
    es = extract_entities("查一下", {"order_id": "99999"})  # 结构化
    assert any(e.entity_type == "order_id" and e.entity_id == "99999" and e.confidence == 1.0 for e in es)  # 恒 1.0


# —— 焦点栈 ——
def test_stack_dedupe_and_cleanup() -> None:  # 去重合并 + 每话题限量
    fs = update_focus_stack(_stack(), [  # 更新
        FocusEntity(entity_type="order_id", entity_id="12345", surface="12345", confidence=1.0, source="regex"),  # 已存在
        FocusEntity(entity_type="order_id", entity_id="88888", surface="88888", confidence=1.0, source="regex"),  # 新
    ])  # 结束
    ids = [e.entity_id for e in fs.stack]  # id 集合
    assert len(ids) == len(set(ids))  # 无重复
    assert "88888" in ids and "12345" in ids  # 都在
    assert fs.turn_index == 3  # 轮次 +1


def test_stack_persists_mentions() -> None:  # 重复提及抬高权重/次数
    fs = update_focus_stack(_stack(), [FocusEntity(entity_type="order_id", entity_id="12345", surface="12345", confidence=1.0, source="regex")])  # 再提
    ent = next(e for e in fs.stack if e.entity_id == "12345")  # 该实体
    assert ent.mention_count >= 2  # 次数 +1


# —— 话题切换 ——
def test_explicit_switch_wins() -> None:  # 显式切换词短路
    assert detect_topic_switch("另外，退款政策是什么", "order", _stack()) == "switch"  # 切换


def test_strong_reference_continue() -> None:  # 强指代反向判延续
    assert detect_topic_switch("它的物流呢", "order", _stack()) == "continue"  # 延续


def test_default_continue() -> None:  # 默认延续
    assert detect_topic_switch("好的", "order", _stack()) == "continue"  # 兜底


# —— 指代消解 ——
def test_explicit_wins() -> None:  # 显式实体优先
    r = resolve_reference("查订单 77777", _stack(), [FocusEntity(entity_type="order_id", entity_id="77777", confidence=1.0, source="regex")])  # 消解
    assert not r.ambiguous and r.bound.entity_id == "77777"  # 绑定显式


def test_unique_in_topic() -> None:  # 唯一候选直接绑
    fs = new_stack()  # 空
    fs.current_topic = "order"  # 话题
    fs.stack = [FocusEntity(entity_type="order_id", entity_id="12345", topic="order", weight=1.0, confidence=1.0, source="regex")]  # 唯一
    r = resolve_reference("它呢", fs, [])  # 消解
    assert not r.ambiguous and r.bound.entity_id == "12345"  # 绑唯一


def test_ordinal_out_of_range_ambiguous() -> None:  # 序数越界 → 歧义
    r = resolve_ordinal("第三个呢", _stack())  # 无列表 → 歧义
    assert r is None or r.ambiguous  # 歧义/不是序数命中


def test_chain_breaks_inherited() -> None:  # 链回溯到原始
    fs = new_stack()  # 空
    here = FocusEntity(entity_type="order_id", entity_id="12345", source="inherited", confidence=1.0, topic="order")  # 中间层
    orig = FocusEntity(entity_type="order_id", entity_id="12345", source="regex", confidence=1.0, topic="order")  # 原始
    fs.stack = [here, orig]  # 两个同名
    back = resolve_with_chain(here, fs)  # 回溯
    assert back is orig  # 回到原始


# —— focus 节点 ——
def test_focus_node_anchor_and_stack() -> None:  # 节点产出锚定 + 更新栈 + 消解后 query
    out = focus_node({"question": "查订单 12345", "preprocessed": {"query": "查订单 12345"}})  # 第一轮
    assert out["anchor_entities"].get("order_id") == "12345"  # 锚定
    assert out["focus_stack"]["stack"]  # 已入栈
    assert out["preprocessed"]["query"]  # 回写 query
    assert out["reference_ambiguous"] is False  # 无歧义


def test_focus_node_second_turn_reference() -> None:  # 第二轮"第二个呢" → 绑定之前建立的列表
    first = focus_node({"question": "查订单 12345 和 67890", "preprocessed": {"query": "查订单 12345 和 67890"}})  # 第一轮（建列表）
    fs = first["focus_stack"]  # 栈（含 group）
    second = focus_node({  # 第二轮
        "question": "第二个呢",  # 序数
        "preprocessed": {"query": "第二个呢"},  # query
        "focus_stack": fs,  # 上一轮栈
    })  # 结束
    assert second["anchor_entities"].get("order_id") == "67890"  # 绑定第二个


def test_focus_node_ambiguous_flag() -> None:  # 无焦点可绑 → 歧义标记
    out = focus_node({"question": "那个呢", "preprocessed": {"query": "那个呢"}})  # 无栈无实体
    assert out["reference_ambiguous"] is True  # 歧义
