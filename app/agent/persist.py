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
        user_id, _assistant_id = conversations.append_turn(  # 原子写一整轮
            conversation,  # 会话
            question=question,  # 问题
            answer=answer,  # 答案
            title=title,  # 标题
            turn_json=payload,  # 形状
        )
        if ctx is not None:  # 有上下文
            ctx.turn_id = str(user_id)  # 逻辑轮次 id = user 消息 id
            ctx.persisted = True  # 标记
        return True  # 成功
    except Exception as exc:  # 写失败
        logger.opt(exception=exc).warning("会话落库失败（{}）", conversation)  # 告警，不阻断
        return False  # 失败
