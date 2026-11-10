"""finalize 节点：落库、发 done、结束 run。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from langchain_core.runnables import RunnableConfig  # 图配置

from app.agent.context import ctx_from_config  # 上下文
from app.agent.events import note as note_event  # 说明事件
from app.agent.persist import persist_turn, persist_turn_async  # 统一落库
from app.agent.stages import stage  # 步骤事件
from app.services import attachments as attachment_service  # 附件公开字段


async def _persist(state: dict[str, Any], ctx: Any) -> None:  # 落库（异步优先）
    """ctx 挂了 AsyncDB 就异步落库（单一异步写者），否则同步。"""
    conversation_id = str(state.get("conversation_id") or "default")  # 会话
    question = str(state.get("question") or "")  # 问题
    answer = str(state.get("answer") or "")  # 答案
    fragments = list(state.get("fragments") or [])  # 引用
    # checkpoint 会按会话保留上一轮的 answer/fragments；本轮若没真正生成（拒绝/仅说明），
    # ctx.partial_answer 为空，必须落空答案并清引用，避免把上一轮旧答案写进新消息。
    if ctx is not None and not ctx.partial_answer and not fragments:  # 本轮未生成
        answer = ""  # 清掉旧答案
        fragments = []  # 清掉旧引用
    note = str(state.get("note") or "")  # 说明
    kwargs = {  # 共用参数
        "conversation_id": conversation_id,  # 会话
        "question": question,  # 问题
        "answer": answer,  # 答案
        "note": note,  # 说明
        "note_kind": str(state.get("note_kind") or "plain"),  # 样式
        "fragments": fragments,  # 引用（本轮未生成时已清空）
        "basis": dict(state.get("basis") or {}),  # 检索依据
        "settings": dict(state.get("settings") or {}),  # 设置
        "usage": dict(state.get("usage") or {}),  # 用量
        "reasoning": str(state.get("reasoning") or ""),  # 思考
        "conflict": str(state.get("conflict") or ""),  # 冲突提示
        "attachments": [attachment_service.public(item) for item in (state.get("attachments") or [])],  # 附件公开
    }  # 参数结束
    if ctx is not None and getattr(ctx, "db", None) is not None:  # 有异步连接
        await persist_turn_async(ctx, **kwargs)  # 异步
    else:  # 同步回退
        persist_turn(ctx, **kwargs)  # 同步


async def finalize_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:  # 收尾节点
    """先落库，再发说明；done 终态事件由 driver 统一发（保证最后一帧）。

    收尾时对工作记忆做 promote：取 key_facts 结论写长期库，随后清空白板（防 checkpoint 膨胀）。
    """
    ctx = ctx_from_config(config)  # 上下文
    with stage("收尾"):  # 步骤（不给 group：只埋 trace，收尾不属于用户步骤）
        await _persist(state, ctx)  # 落库（异步优先）
        note = str(state.get("note") or "")  # 说明（只进 state）
        if note and ctx is not None:  # 有说明且有 run（说明事件由 driver 的 REPLACE/NOTE 兜底，这里只补一手）
            ctx.emit(note_event(note))  # 发说明
    update: dict[str, Any] = {}  # 状态更新
    white = state.get("working_memory")  # 工作记忆
    if isinstance(white, dict) and white:  # 有白板
        from contextlib import suppress  # 抑制写失败

        from app.agent.memory import blank_working, promote_working  # 收尾白板
        from app.agent.memory.longterm import MemoryFact  # 事实形状
        from app.agent.memory.manager import record_facts  # 记忆统一写口

        _past, key_facts = promote_working(white)  # 取结论候选（步骤+关键事实）
        if key_facts:  # 有结论
            with suppress(Exception):  # 写失败不影响收尾
                record_facts(  # 统一写回（去重 + PII 脱敏）
                    [MemoryFact(key=str(key), value=value, source="verified") for key, value in key_facts.items()],  # 已验证结论
                    redact_pii=True,  # 脱敏
                    conversation_id=str(state.get("conversation_id") or ""),  # 来源会话
                )  # 写
        update["working_memory"] = blank_working()  # 清空白板（promote 后不再保留）
    # —— 蒸馏排程：异步从会话压缩段抽事实写长期库（不打主线；jobs 未初始化时安静跳过）——
    conv_id = str(state.get("conversation_id") or "")  # 会话
    if conv_id:  # 有会话才值得蒸馏
        from contextlib import suppress as _suppress  # 抑制

        with _suppress(Exception):  # 入队失败（jobs 未初始化等）不影响收尾
            from app.core.jobs import api as jobs_api  # 任务接口
            from app.tasks.memory_distill import MemoryDistill  # 蒸馏任务

            jobs_api.enqueue(  # 排队（幂等键：同会话去重）
                MemoryDistill.key(),  # 任务 key
                {"conversation_id": conv_id},  # 参数
                dedupe_key=f"memory_distill:{conv_id}",  # 去重
            )  # 入队
    return update  # 返回
