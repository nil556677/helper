"""焦点 · 栈维护：每轮去重/入栈、时间衰减、四维清理、有序列表独立过期。

- `new_stack()` 建空白栈；`update_focus_stack()` 每轮更新（决策 → 合并新实体 → 清理 → 衰减）。
- 纯数据函数，可单测；随 `ChatState.focus_stack` 同步（checkpointer 持久化）。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from app.agent.contracts.focus import FocusEntity, FocusStack  # 契约

__all__ = ["MAX_GROUP_AGE_TURNS", "MAX_PER_TOPIC", "add_entities", "cleanup_stack", "new_stack", "update_focus_stack"]  # 导出

#: 清理/衰减阈值（外置可配；当初值用）。
MAX_AGE_TURNS = 5  # 未提及 N 轮淘汰
MAX_PER_TOPIC = 5  # 每话题上限
MAX_GROUP_AGE_TURNS = 3  # 有序列表过期
DECAY_FACTOR = 0.8  # 每轮衰减
DECAY_FLOOR = 0.2  # 衰减下限
LOW_CONFIDENCE_THRESHOLD = 0.5  # 低置信阈值


def new_stack() -> FocusStack:  # 空白焦点栈
    return FocusStack()  # 空


def update_focus_stack(  # 每轮更新
    fs: FocusStack,  # 现有栈（就地改）
    new_entities: list[FocusEntity],  # 本轮新实体
    topic_decision: str = "continue",  # switch/continue/uncertain
    new_topic: str = "",  # 新话题
) -> FocusStack:  # 更新后的栈
    from app.agent.preprocess.focus.topic import apply_topic_decision  # 话题决策

    fs.turn_index += 1  # 轮次 +1
    apply_topic_decision(fs, topic_decision, new_topic)  # 话题决策
    add_entities(fs, new_entities)  # 新实体入栈（去重合并）
    cleanup_stack(fs)  # 清理 + 衰减
    return fs  # 返回


def add_entities(fs: FocusStack, new_entities: list[FocusEntity]) -> None:  # 新实体入栈（去重合并）
    for e in new_entities:  # 逐个
        existing = _find(fs, str(e.entity_type), str(e.entity_id))  # 找同类型同值
        if existing is not None:  # 已有 → 更新提及
            existing.last_mentioned_at = fs.turn_index  # 最近提及
            existing.mention_count += 1  # 次数
            existing.weight = min(1.0, existing.weight + 0.2)  # 权重抬升
            existing.confidence = max(existing.confidence, e.confidence)  # 置信取高
            continue  # 下一个
        e.introduced_at = fs.turn_index  # 引入轮次
        e.last_mentioned_at = fs.turn_index  # 最近提及
        e.mention_count = 1  # 首次
        e.weight = 1.0  # 新实体满权重
        e.topic = e.topic or fs.current_topic  # 打话题
        fs.stack.append(e)  # 入栈


def cleanup_stack(fs: FocusStack) -> None:  # 四维清理 + 时间衰减
    fs.stack = _evict_age(fs.stack, fs.turn_index)  # 时间
    fs.stack = _evict_capacity(fs.stack)  # 容量
    fs.stack = _evict_low_confidence(fs.stack)  # 低置信
    fs.groups = _evict_groups(fs.groups, fs.turn_index)  # 列表过期
    for e in fs.stack:  # 衰减
        age = fs.turn_index - e.last_mentioned_at  # 未提及轮数
        e.weight = max(DECAY_FLOOR, DECAY_FACTOR ** max(age, 0))  # 衰减公式


def _find(fs: FocusStack, etype: str, eid: str) -> FocusEntity | None:  # 找同键
    for e in fs.stack:  # 逐个
        if str(e.entity_type) == etype and str(e.entity_id) == eid:  # 同键
            return e  # 命中
    return None  # 无


def _evict_age(rows: list[FocusEntity], turn: int) -> list[FocusEntity]:  # 时间淘汰
    return [e for e in rows if turn - e.last_mentioned_at <= MAX_AGE_TURNS]  # 保留近期


def _evict_capacity(rows: list[FocusEntity]) -> list[FocusEntity]:  # 每话题限量
    kept: list[FocusEntity] = []  # 结果
    per_topic: dict[str, int] = {}  # 各话题计数
    for e in sorted(rows, key=lambda x: (x.weight, x.last_mentioned_at), reverse=True):  # 按权重/最近排序
        topic = e.topic or "?"  # 话题
        if per_topic.get(topic, 0) >= MAX_PER_TOPIC:  # 超上限
            continue  # 淘汰
        per_topic[topic] = per_topic.get(topic, 0) + 1  # 计数
        kept.append(e)  # 保留
    return kept  # 返回


def _evict_low_confidence(rows: list[FocusEntity]) -> list[FocusEntity]:  # 低置信且少提及淘汰
    return [  # 保留
        e for e in rows  # 逐个
        if e.confidence >= LOW_CONFIDENCE_THRESHOLD or e.mention_count >= 2  # 高置信或重复提及
    ]  # 结束


def _evict_groups(groups: list[Any], turn: int) -> list[Any]:  # 有序列表过期
    return [g for g in groups if turn - int(getattr(g, "established_at", 0)) <= MAX_GROUP_AGE_TURNS]  # 保留近期
