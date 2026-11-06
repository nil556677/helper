"""L1 预处理节点：把原始问题规范化为可检索/可分诊的 `preprocessed`。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.contracts.triage import EntityBundle, PreprocessResult  # 契约
from app.agent.preprocess.entities import extract_entities, inherit_from_turns  # 实体
from app.agent.preprocess.flags import collect_flags  # flags
from app.agent.preprocess.rewrite import maybe_rewrite  # 改写接缝
from app.agent.preprocess.timeparse import resolve_time  # 时间

__all__ = ["preprocess_node"]  # 导出


def preprocess_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """零 LLM：抽实体、解析时间、收 flags、按需一次改写；写 `preprocessed`。"""
    del config  # 不用图配置
    raw = str(state.get("question") or "")  # 原始问题
    turns = state.get("turns") or []  # 历史
    flags = collect_flags(state.get("flags") if isinstance(state.get("flags"), dict) else {})  # 硬约束
    entities = EntityBundle(explicit=extract_entities(raw), inherited=inherit_from_turns(turns))  # 实体
    time_range = resolve_time(raw)  # 时间范围（可能 None）
    query, rewritten, reason = maybe_rewrite(raw, entities, flags, None)  # 改写接缝（模型在 M3 接）
    pending = state.get("pending_clarify")  # 上轮待澄清
    phase = "followup" if pending else ("middle" if turns else "start")  # 阶段
    result = PreprocessResult(  # 产物
        query=query,  # 规范化 query
        entities=entities,  # 实体
        time_range=time_range,  # 时间范围
        flags=flags,  # flags
        rewritten=rewritten,  # 是否改写
        rewrite_reason=reason,  # 原因
        phase=phase,  # 阶段
        pending_clarify=pending if isinstance(pending, dict) else None,  # 待澄清
    )  # 结束
    return {"preprocessed": result.model_dump(mode="json"), "question": query}  # 写回