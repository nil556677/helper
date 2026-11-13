"""焦点 · 指代消解：显式优先 / 规则链 / 序数 / 链回溯 / 歧义。

- `resolve_reference(query, fs, new_entities)` 主入口；
- `resolve_entity_reference` / `resolve_ordinal` / `resolve_with_chain` 子规则；
- 歧义不猜 → 返回 `ResolutionResult(ambiguous=True, candidates=...)`，由节点走澄清。
"""

from __future__ import annotations  # 延迟值注解

from app.agent.contracts.focus import FocusEntity, FocusStack, ResolutionResult  # 契约

__all__ = ["ORDINAL_MAP", "REFERENCE_WORDS", "resolve_entity_reference", "resolve_ordinal", "resolve_reference", "resolve_with_chain"]  # 导出

#: 序数词 → 索引（-1=最后一个）。
ORDINAL_MAP: dict[str, int] = {  # 映射
    "第一个": 0, "第1个": 0, "前者": 0,  # 0
    "第二个": 1, "第2个": 1, "后者": 1,  # 1
    "第三个": 2, "第3个": 2,  # 2
    "最后一个": -1, "倒数第二个": -2, "前一个": -1, "后一个": 1,  # 相对
}  # 结束

#: 指代词（需走分支判断）。
REFERENCE_WORDS: tuple[str, ...] = ("它", "他", "这个", "那个", "该", "其", "刚才", "上面那个", "第二个", "第一个", "最后一个", "前者", "后者", "倒数第")  # 指示词

#: 有序列表最大年龄。
MAX_GROUP_AGE_TURNS = 3  # 轮


def resolve_reference(  # 主入口
    query: str,  # query
    fs: FocusStack | None,  # 焦点栈
    new_entities: list[FocusEntity],  # 本轮新实体
) -> ResolutionResult:  # 结果
    """显式实体(自足) → 直接绑；否则序数/指代词 → 对焦点栈消解；再无 → 歧义。"""
    fs = fs or FocusStack()  # 兜底空栈
    text = str(query or "")  # 文本
    # 显式实体：本轮有高置信实体 → query 自足，直接绑首个（也用于入栈）
    explicit = [e for e in new_entities if e.confidence >= 0.8]  # 高置信
    if explicit and not _is_reference_only(text):  # 自足且非纯指代
        return ResolutionResult(bound=explicit[0], source="explicit")  # 直接绑
    # 序数优先
    ordinal_res = resolve_ordinal(text, fs)  # 序数
    if ordinal_res is not None:  # 是序数
        return ordinal_res  # 返回
    # 指代词 → 实体规则链
    if any(w in text for w in REFERENCE_WORDS):  # 有指代
        return resolve_entity_reference(text, fs)  # 实体消解
    # 无显式无指代：可能是"省略主语"，当无焦点可绑 → 歧义
    return ResolutionResult(ambiguous=True, reason="no_candidate", candidates=[])  # 歧义


def resolve_entity_reference(  # 实体指代：规则链
    query: str,  # query
    fs: FocusStack,  # 焦点栈
) -> ResolutionResult:  # 结果
    candidates = [e for e in fs.stack if not e.topic or e.topic == fs.current_topic]  # 当前话题候选
    if not candidates:  # 无候选
        return ResolutionResult(ambiguous=True, reason="no_candidate", candidates=[])  # 歧义
    if len(candidates) == 1:  # 唯一
        return ResolutionResult(bound=candidates[0], source="unique")  # 直接绑
    # 多候选
    if ("这个" in query or "那个" in query or "该" in query):  # 指示词
        recent = max(candidates, key=lambda e: e.last_mentioned_at)  # 最近
        if fs.turn_index - recent.last_mentioned_at <= 1:  # 刚提过
            return ResolutionResult(bound=recent, source="proximal")  # 绑最近
    if "刚才" in query:  # 刚才那个
        return ResolutionResult(bound=max(candidates, key=lambda e: e.last_mentioned_at), source="recent")  # 绑最近
    # 按 (weight, last_mentioned) 排序
    ranked = sorted(candidates, key=lambda e: (e.weight, e.last_mentioned_at), reverse=True)  # 排序
    best = ranked[0]  # 最佳
    second = ranked[1]  # 次佳
    if best.weight - second.weight >= 0.3:  # 明显领先
        return ResolutionResult(bound=best, source="weighted")  # 绑最佳
    return ResolutionResult(ambiguous=True, candidates=ranked[:3], reason="tie")  # 无法区分


def resolve_ordinal(  # 序数指代
    query: str,  # query
    fs: FocusStack,  # 焦点栈
) -> ResolutionResult | None:  # 结果或 None（非序数）
    idx = _parse_ordinal(query)  # 索引或 None
    if idx is None:  # 非序数
        return None  # 空
    group = None  # 目标列表
    for g in fs.groups:  # 降序、最新在前
        if fs.turn_index - int(getattr(g, "established_at", 0)) > MAX_GROUP_AGE_TURNS:  # 过期
            continue  # 下一个
        if getattr(g, "topic", "") and g.topic != fs.current_topic:  # 话题不符
            continue  # 下一个
        group = g  # 命中
        break  # 停
    if group is None:  # 无有效列表
        return ResolutionResult(ambiguous=True, reason="no_valid_group", candidates=[])  # 歧义
    items = list(getattr(group, "items", None) or [])  # 条目
    try:  # 取索引
        bound = items[idx]  # 条目
    except IndexError:  # 越界
        return ResolutionResult(ambiguous=True, reason="ordinal_out_of_range", candidates=items[:3])  # 歧义
    return ResolutionResult(bound=bound, source="ordinal", inherited_intent=str(getattr(group, "original_intent", "") or ""))  # 绑


def resolve_with_chain(  # 链回溯：source=inherited 回到原始实体
    entity: FocusEntity,  # 起始
    fs: FocusStack,  # 栈
) -> FocusEntity:  # 原始实体
    cur = entity  # 当前
    seen: set[str] = set()  # 防循环
    while str(cur.source) == "inherited" and str(cur.entity_id) not in seen:  # 该回溯
        seen.add(str(cur.entity_id))  # 去重
        parent = _find_by_id(fs, str(cur.entity_id))  # 找父
        if parent is None:  # 找不到了
            break  # 停
        cur = parent  # 上溯
    return cur  # 返回


def _find_by_id(fs: FocusStack, eid: str) -> FocusEntity | None:  # 按 id 找父（含 inherited 层）
    for e in fs.stack:  # 逐个
        if str(e.entity_id) == eid and str(e.source) != "inherited":  # 非中间层
            return e  # 返回
    return None  # 无


def _is_reference_only(query: str) -> bool:  # 是否纯指代/省略句（无新动作词）
    return any(w in query for w in (ORDINAL_MAP)) or "呢" in query or not query  # 判断


def _parse_ordinal(query: str) -> int | None:  # 解析序数索引
    for word, idx in ORDINAL_MAP.items():  # 映射
        if word in query:  # 命中
            return idx  # 返回
    return None  # 无
