"""一轮答复落库：统一 turn_json 形状、原子写、fail-open（normal / 取消 / 失败共用）。

为什么要收成一处：`stream._persist`（取消/失败）与 `finalize`（正常）各写过一份，
字段与顺序迟早漂移；这里只保留一份“形状 + 写入”，两边都调它。
"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from loguru import logger  # 日志

from app.agent.steps import grouped  # 用户级步骤
from app.services import attachments as attachment_service  # 附件公开字段
from app.services import conversations  # 会话落库

__all__ = ["build_turn_json", "persist_turn"]  # 对外导出


def build_turn_json(  # 统一形状
    *,
    answer: str,  # 答案正文
    note: str = "",  # 说明
    note_kind: str = "plain",  # 说明样式
    fragments: list[dict[str, Any]] | None = None,  # 引用
    steps: list[dict[str, Any]] | None = None,  # 用户级步骤
    basis: dict[str, Any] | None = None,  # 检索依据
    settings: dict[str, Any] | None = None,  # 本轮设置
    usage: dict[str, Any] | None = None,  # 用量
    reasoning: str = "",  # 思考
    conflict: str = "",  # 冲突提示
    context_usage: dict[str, Any] | None = None,  # 上下文分块占用
    attachments: list[dict[str, Any]] | None = None,  # 附件公开字段
) -> dict[str, Any]:  # turn_json
    """字段只在这里拼；两边落库都复用它。"""
    return {  # 结果
        "answer": answer,  # 答案
        "note": note,  # 说明
        "note_kind": note_kind,  # 样式
        "fragments": list(fragments or []),  # 引用
        "steps": list(steps or []),  # 步骤
        "basis": dict(basis or {}),  # 检索依据
        "settings": dict(settings or {}),  # 设置
        "usage": dict(usage or {}),  # 用量
        "reasoning": reasoning,  # 思考
        "conflict": conflict,  # 冲突
        "context_usage": dict(context_usage or {}),  # 占用
        "attachments": list(attachments or []),  # 附件
    }  # 返回


def persist_turn(  # 写一轮
    ctx: Any,  # RunContext（可为 None：旧路径）
    *,
    conversation_id: str,  # 会话
    question: str,  # 问题
    answer: str,  # 答案（可为空）
    note: str = "",  # 说明
    note_kind: str = "plain",  # 说明样式
    fragments: list[dict[str, Any]] | None = None,  # 引用
) -> bool:  # 是否写成功
    """原子写 user+assistant；成功置 ``ctx.persisted``，失败记日志返回 False。"""
    if ctx is not None and ctx.persisted:  # 已经落过
        return True  # 幂等
    conversation = conversation_id or "default"  # 会话兜底
    title = question[:18] if question else conversations.DEFAULT_TITLE  # 标题
    steps = grouped(list(ctx.steps)) if ctx is not None else []  # 步骤
    payload = build_turn_json(  # 统一形状
        answer=answer,  # 答案
        note=note,  # 说明
        note_kind=note_kind,  # 样式
        fragments=fragments or [],  # 引用
        steps=steps,  # 步骤
        basis=dict(ctx.basis) if ctx is not None else {},  # 检索依据
        settings=dict(ctx.settings) if ctx is not None else {},  # 设置
        usage=dict(ctx.usage) if ctx is not None else {},  # 用量
        reasoning=ctx.reasoning if ctx is not None else "",  # 思考
        conflict=ctx.conflict if ctx is not None else "",  # 冲突
        context_usage=(  # 上下文占用
            ctx.context_usage.to_dict()  # 序列化
            if ctx is not None and ctx.context_usage is not None  # 有
            else {}  # 无
        ),
        attachments=[attachment_service.public(item) for item in (ctx.attachments if ctx is not None else [])],  # 附件
    )  # 形状结束
    try:  # 写失败只告警
        # 发送即落库：HTTP POST 已预写 user 消息（带 run_id）时，这里只补 assistant、不重复插 user。
        linked = None  # 命中的预写 user
        if ctx is not None and ctx.run_id:  # 有 run 上下文
            linked = conversations.pending_user_by_run(conversation, ctx.run_id)  # 查预写
        if linked is not None:  # 已预写
            user_id, _assistant_id = conversations.append_turn_existing_user(  # 只补 assistant
                conversation,  # 会话
                turn_id=str(linked["turn_id"]),  # 轮次 id（= 预写 user id）
                answer=answer,  # 答案
                title=title,  # 标题
                turn_json=payload,  # 形状
            )  # 写
        else:  # 未预写 → 原子写一整轮（非 HTTP 路径 / 测试）
            user_id, _assistant_id = conversations.append_turn(  # 原子写一整轮
                conversation,  # 会话
                question=question,  # 问题
                answer=answer,  # 答案
                title=title,  # 标题
                turn_json=payload,  # 形状
            )  # 写
        if ctx is not None:  # 有上下文
            ctx.turn_id = str(user_id)  # 逻辑轮次 id = user 消息 id
            ctx.persisted = True  # 标记
        return True  # 成功
    except Exception as exc:  # 写失败
        logger.opt(exception=exc).warning("会话落库失败（{}）", conversation)  # 告警，不阻断
        return False  # 失败


async def persist_turn_async(  # 异步落库（单一异步写者）
    ctx: Any,  # RunContext
    *,
    conversation_id: str,  # 会话
    question: str,  # 问题
    answer: str,  # 答案
    note: str = "",  # 说明
    note_kind: str = "plain",  # 样式
    fragments: list[dict[str, Any]] | None = None,  # 引用
) -> bool:  # 是否写成功
    """经 ``ctx.db``（AsyncDB）异步落库；ctx.db 为空则回退同步 ``persist_turn``。"""
    from app.core.db.asyncstore import ConversationAsyncStore  # 异步仓储

    if ctx is None or ctx.db is None:  # 没有异步连接
        return persist_turn(  # 回退同步
            ctx, conversation_id=conversation_id, question=question, answer=answer,
            note=note, note_kind=note_kind, fragments=fragments,
        )  # 回退结束
    if ctx.persisted:  # 幂等
        return True  # 已落
    conversation = conversation_id or "default"  # 会话兜底
    title = question[:18] if question else conversations.DEFAULT_TITLE  # 标题
    steps = grouped(list(ctx.steps))  # 步骤
    payload = build_turn_json(  # 统一形状
        answer=answer, note=note, note_kind=note_kind, fragments=fragments or [], steps=steps,
        basis=dict(ctx.basis), settings=dict(ctx.settings), usage=dict(ctx.usage),
        reasoning=ctx.reasoning, conflict=ctx.conflict,
        context_usage=(ctx.context_usage.to_dict() if ctx.context_usage is not None else {}),
        attachments=[attachment_service.public(item) for item in ctx.attachments],
    )  # 形状结束
    store = ConversationAsyncStore(ctx.db)  # 异步仓储
    try:  # 写失败只告警
        linked = None  # 预写 user
        if ctx.run_id:  # 有 run
            linked = await store.pending_user_by_run(conversation, ctx.run_id)  # 查预写
        if linked is not None:  # 已预写
            user_id, _assistant_id = await store.append_turn_existing_user(  # 只补 assistant
                conversation, turn_id=str(linked["turn_id"]), answer=answer,
                title=title, turn_json=payload,
            )  # 写
        else:  # 未预写
            user_id, _assistant_id = await store.append_turn(  # 整轮
                conversation, question=question, answer=answer, title=title, turn_json=payload,
            )  # 写
        ctx.turn_id = str(user_id)  # 轮次 id
        ctx.persisted = True  # 标记
        return True  # 成功
    except Exception as exc:  # 写失败
        logger.opt(exception=exc).warning("会话落库失败（异步 {}）", conversation)  # 告警
        return False  # 失败
