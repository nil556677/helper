"""L1 预处理节点：规范化 query + 多轮指代消解/焦点栈（同一节点内完成）。

把焦点逻辑合进 preprocess（而非单独图节点），避免在热路由链上新增分支导致 LangGraph 调度
把 `route` 多节点写进同一 superstep（见历史问题）。State 通道 focus_stack/slots 随 checkpointer
跨轮持久化；产出 resolved_query/anchor_entities 供路由/执行消费。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from langchain_core.runnables import RunnableConfig  # 节点 config

from app.agent.contracts.triage import EntityBundle, PreprocessResult  # 契约
from app.agent.preprocess.entities import extract_entities as extract_legacy  # 旧实体
from app.agent.preprocess.entities import inherit_from_turns
from app.agent.preprocess.flags import collect_flags  # flags
from app.agent.preprocess.focus.extract import extract_entities as extract_focus  # 焦点抽取
from app.agent.preprocess.focus.resolve import resolve_reference  # 指代消解
from app.agent.preprocess.focus.stack import new_stack, update_focus_stack  # 焦点栈
from app.agent.preprocess.focus.topic import detect_topic_switch, topic_scores  # 话题

__all__ = ["preprocess_node"]  # 导出


def preprocess_node(state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:  # 图节点
    """零 LLM。抽实体、解析时间、收 flags、焦点管理（多轮指代），写 `preprocessed` + 焦点字段。"""
    del config  # 不用图配置
    raw = str(state.get("question") or "")  # 原始问题
    turns = state.get("turns") or []  # 历史
    flags = collect_flags(state.get("flags") if isinstance(state.get("flags"), dict) else {})  # 硬约束
    entities = EntityBundle(explicit=extract_legacy(raw), inherited=inherit_from_turns(turns))  # 旧实体
    time_range = _resolve_time(raw)  # 时间范围（可能 None）
    pending = state.get("pending_clarify")  # 上轮待澄清
    phase = "followup" if pending else ("middle" if turns else "start")  # 阶段
    # —— 焦点管理（多轮指代消解）——
    focus_out = _run_focus(state, raw, entities)  # 焦点产出
    query = focus_out["resolved_query"]  # 消解后 query（路由/下游用）
    result = PreprocessResult(  # 产物
        query=query,  # 消解后 query
        entities=entities,  # 实体包
        time_range=time_range,  # 时间范围
        flags=flags,  # flags
        phase=phase,  # 阶段
        pending_clarify=pending if isinstance(pending, dict) else None,  # 待澄清
    )  # 结束
    result_dict = result.model_dump(mode="json")  # 序列化
    result_dict["resolved_query"] = query  # 消解 query（产物带）
    result_dict["anchor_entities"] = focus_out["anchor_entities"]  # 锚定实体
    return {  # 写回
        "preprocessed": result_dict,  # 预处理产物（含焦点字段）
        "question": query,  # 消解后作为本轮问题
        "focus_stack": focus_out["focus_stack"],  # 焦点栈（同步随 State）
        "resolved_query": query,  # 消解 query
        "anchor_entities": focus_out["anchor_entities"],  # 锚定实体
        "reference_ambiguous": focus_out["reference_ambiguous"],  # 是否歧义
        "inherited_intent": focus_out["inherited_intent"],  # 序数继承意图
    }  # 结束


def _run_focus(  # 焦点管理主体（纯逻辑，可单测）
    state: dict[str, Any],  # 图状态
    raw: str,  # 原始问题
    entities: EntityBundle,  # 旧实体包
) -> dict[str, Any]:  # 焦点产出
    from app.agent.preprocess.focus import store  # 审计

    fs = _load_stack(state)  # 焦点栈（没有则空）
    structured = _structured_entities(entities)  # 结构化实体
    new_entities = extract_focus(raw, structured)  # 抽取新实体
    _establish_groups(fs, new_entities, raw)  # 建有序列表
    scores = topic_scores(raw)  # 词典打分
    new_domain = max(scores, key=scores.get) if scores else ""  # 最高话题
    decision = detect_topic_switch(raw, fs.current_topic, fs, new_domain=new_domain)  # 话题决策
    result = resolve_reference(raw, fs, new_entities)  # 指代消解
    fs = update_focus_stack(fs, new_entities, topic_decision=decision, new_topic=new_domain)  # 更新栈
    anchor: dict[str, Any] = {}  # 锚定
    if result.bound is not None:  # 绑定了
        anchor[str(result.bound.entity_type)] = str(result.bound.entity_id)  # 锚定
    resolved = _build_resolved(raw, result)  # 消解 query
    _record(store, state, fs, new_entities, result)  # 审计（best-effort）
    return {  # 产出
        "focus_stack": fs.model_dump(mode="json"),  # 栈
        "anchor_entities": anchor,  # 锚定
        "reference_ambiguous": result.ambiguous,  # 歧义
        "inherited_intent": result.inherited_intent or "",  # 继承意图
        "resolved_query": resolved,  # 消解 query
    }  # 结束


def _load_stack(state: dict[str, Any]) -> Any:  # 读焦点栈
    from app.agent.contracts.focus import FocusStack  # 契约

    raw_stack = state.get("focus_stack")  # 栈 JSON
    if isinstance(raw_stack, dict) and raw_stack:  # 有
        try:  # 容错
            return FocusStack.model_validate(raw_stack)  # 解析
        except Exception:  # 坏数据
            return new_stack()  # 空
    return new_stack()  # 空


def _structured_entities(bundle) -> dict[str, Any]:  # 取结构化实体
    merged = dict(bundle.explicit if hasattr(bundle, "explicit") else {})  # 显式
    inherited = bundle.inherited if hasattr(bundle, "inherited") else {}  # 继承
    for k, v in inherited.items():  # 补继承
        merged.setdefault(k, v)  # 不覆盖
    return merged  # 返回


def _establish_groups(fs: Any, entities: list[Any], query: str) -> None:  # 建有序列表
    from app.agent.contracts.focus import OrderedGroup  # 契约

    if len(entities) < 2:  # 不足
        return  # 不建
    items = [e for e in entities if str(e.entity_type) in ("order_id", "tracking_no")]  # 数字类
    if len(items) < 2:  # 不足
        return  # 不建
    fs.groups.insert(0, OrderedGroup(  # 最新在前
        group_id=f"g{fs.turn_index + 1}",  # 组 id
        label="命中列表",  # 标签
        items=list(items),  # 条目
        established_at=fs.turn_index + 1,  # 轮
        original_intent=_guess_intent(query),  # 意图
        source="user_enumeration",  # 来源
        topic=fs.current_topic,  # 话题
    ))  # 插入


def _guess_intent(query: str) -> str:  # 粗略意图（序数继承用）
    if any(w in query for w in ("退款", "退货")):  # 退款
        return "query_refund"  # 意图
    if any(w in query for w in ("物流", "快递", "运单")):  # 物流
        return "query_logistics"  # 意图
    return "query_order_status"  # 默认订单


def _build_resolved(query: str, result: Any) -> str:  # 消解后 query（保义，只加锚定标记）
    if result.bound is None or result.source == "explicit":  # 无绑定或显式自足
        return query  # 原样
    return f"{query} [{result.bound.entity_type}：{result.bound.entity_id}]"  # 追加锚定标记


def _resolve_time(raw: str) -> Any:  # 时间解析（容错）
    from app.agent.preprocess.timeparse import resolve_time  # 时间

    return resolve_time(raw)  # 返回


def _record(store, state: dict[str, Any], fs: Any, new_entities: list[Any], result: Any) -> None:  # 审计（best-effort）
    """记 entity_added / binding_resolved / binding_ambiguous；无 thread 或失败都静默。"""
    thread = str(state.get("thread_id") or "")  # 线程
    if not thread:  # 无线程
        return  # 跳过
    try:  # 失败不阻断
        turn = int(getattr(fs, "turn_index", 0))  # 轮
        for e in new_entities:  # 逐个
            store.record_event(thread_id=thread, turn_index=turn, event_type="entity_added",  # 基础
                               entity_type=str(e.entity_type), entity_id=str(e.entity_id),  # 实体
                               topic=str(e.topic), confidence=float(e.confidence))  # 话题/置信
        if result.bound is not None:  # 有绑定
            store.record_event(thread_id=thread, turn_index=turn, event_type="binding_resolved",  # 基础
                               entity_type=str(result.bound.entity_type), entity_id=str(result.bound.entity_id),  # 实体
                               topic=str(result.bound.topic), confidence=float(result.bound.confidence),  # 话题/置信
                               detail={"source": result.source, "candidates": [  # 依据+候选
                                   {"id": c.entity_id, "type": c.entity_type} for c in (result.candidates or [])  # 列表
                               ]})  # 结束
        elif result.ambiguous:  # 歧义
            store.record_event(thread_id=thread, turn_index=turn, event_type="binding_ambiguous",  # 基础
                               detail={"reason": result.reason, "candidates": [  # 原因+候选
                                   {"id": c.entity_id, "type": c.entity_type} for c in (result.candidates or [])  # 列表
                               ]})  # 结束
    except Exception:  # 审计失败不阻断
        return  # 忽略
