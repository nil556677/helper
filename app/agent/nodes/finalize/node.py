"""finalize 节点：落库、发 done、结束 run。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from langchain_core.runnables import RunnableConfig  # 图配置

from app.agent.context import ctx_from_config  # 上下文
from app.agent.events import note as note_event  # 说明事件
from app.agent.persist import persist_turn  # 统一落库
from app.agent.stages import stage  # 步骤事件


def _persist(state: dict[str, Any], ctx: Any) -> None:  # 落库
    """与取消/失败路径共用同一套形状与原子写（不会出现半轮）。"""
    conversation_id = str(state.get("conversation_id") or "default")  # 会话
    question = str(state.get("question") or "")  # 问题
    answer = str(state.get("answer") or "")  # 答案
    note = str(state.get("note") or (ctx.note if ctx is not None else ""))  # 说明
    persist_turn(  # 统一落库
        ctx,  # 上下文
        conversation_id=conversation_id,  # 会话
        question=question,  # 问题
        answer=answer,  # 答案
        note=note,  # 说明
        note_kind=str(state.get("note_kind") or "plain"),  # 样式
        fragments=list(state.get("fragments") or []),  # 引用
    )


async def finalize_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:  # 收尾节点
    """先落库，再发说明；done 终态事件由 driver 统一发（保证最后一帧）。"""
    ctx = ctx_from_config(config)  # 上下文
    with stage("收尾"):  # 步骤（不给 group：只埋 trace，收尾不属于用户步骤）
        _persist(state, ctx)  # 落库
        if ctx is not None and ctx.note:  # 新链路且有说明
            ctx.emit(note_event(ctx.note))  # 发说明
    return {}  # 不改状态
