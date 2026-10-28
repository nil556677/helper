"""load_context 节点：读会话历史（含压缩摘要投影），token 裁剪，必要时压缩。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from langchain_core.runnables import RunnableConfig  # 图配置

from app.agent.context import ctx_from_config  # 上下文
from app.agent.contexts.budget import Budget  # 预算
from app.agent.contexts.compact import compact  # 压缩引擎
from app.agent.contexts.history import (  # 历史投影
    messages_from,  # 轮次 -> 消息
    turns_from_rows,  # 行 -> 轮次
)  # 导入结束
from app.agent.contexts.history import (
    trim as trim_history,  # token 裁剪
)
from app.agent.contexts.summarize import build_summarizer  # 摘要生成器（不可用返回 None）
from app.agent.contexts.tokens import count_messages  # token 估算
from app.agent.contracts import StageGroup, TurnStatus  # 契约（分组/轮次状态）
from app.agent.events import context as context_event  # 事件
from app.agent.fsm.states import RunEvent  # 状态事件
from app.agent.stages import stage  # 步骤事件
from app.services import (  # 服务
    attachments,  # 附件
    conversations,  # 会话服务
)  # 导入结束

MAX_HISTORY_MESSAGES = 200  # 读取上限（I/O 安全阀）；真正收紧靠 token 裁剪


async def load_context(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:  # 上下文节点
    """返回 turns 与滚动摘要；状态机进入 retrieving。"""
    ctx = ctx_from_config(config)  # 上下文
    conversation_id = str(state.get("conversation_id") or "")  # 会话 id
    with stage("准备对话", group=StageGroup.PREPARE):  # 步骤
        snapshot: dict[str, Any] = {"summary": "", "until_message_id": 0}  # 默认投影头
        rows: list[dict[str, Any]] = []  # 原始行
        if conversation_id:  # 有会话
            try:  # 库异常不阻断
                snapshot = conversations.load_summary(conversation_id)  # 读摘要投影头
                rows = conversations.load_messages(conversation_id)  # 读全部消息
            except Exception:  # 读失败
                snapshot = {"summary": "", "until_message_id": 0}  # 复位
                rows = []  # 空历史
        summary_text = str(snapshot.get("summary") or "")  # 滚动摘要
        since = int(snapshot.get("until_message_id") or 0)  # 摘要覆盖终点
        turns = turns_from_rows(since, rows, limit=MAX_HISTORY_MESSAGES)  # 成对轮次
        messages = messages_from(turns, with_ids=True)  # 压缩/裁剪用消息投影
        files = list(state.get("attachments") or [])  # 附件（正文不入 state）
        if files:  # 有附件
            with stage("读取附件", group=StageGroup.PREPARE, name="attachments") as info:  # 附件步骤
                readable = sum(1 for item in files if attachments.load_text(item).strip())  # 按 disk 读判可读
                info.detail = f"{len(files)} 个附件，{readable} 个可读"  # 明细
        if ctx is not None:  # 新链路
            budget = Budget()  # 预算
            if count_messages(messages) > budget.history_budget:  # T1 软触发（历史超预算）
                compactor = build_summarizer(ctx.emit)  # 摘要生成器
                if compactor is not None:  # 模型可用
                    result = await compact(  # 压缩最老一段
                        messages,  # 全部历史
                        compactor,  # 生成器
                        summary_text=summary_text,  # 旧摘要
                        budget=budget,  # 预算
                    )  # 压缩结束
                    if result is not None:  # 摘要被接受
                        conversations.save_summary(  # 落库（滚动替换）
                            conversation_id,  # 会话
                            result.until_message_id,  # 覆盖终点
                            result.summary,  # 新摘要
                        )  # 落库结束
                        summary_text = result.summary  # 本轮就用新摘要
            trimmed, dropped, _saved = trim_history(messages, budget)  # 兜底裁剪（到 trim_target）
            messages = trimmed  # 用裁剪后的历史
            estimate = count_messages(messages)  # 统一估算
            ctx.emit(  # 上下文事件
                context_event(  # 构造
                    len(messages),  # 条数
                    estimate,  # token
                    trimmed=dropped,  # 裁剪条数
                    window=budget.window,  # 窗口
                )  # 事件结束
            )  # emit 结束
            ctx.transition(RunEvent.CONTEXT_LOADED)  # 状态
        kept_ids = {int(item["id"]) for item in messages if "id" in item}  # 裁剪后保留的轮次 id
        turns = [t for t in turns if t.status is TurnStatus.DONE and int(t.turn_id) in kept_ids]  # 只留保留轮次
    return {"turns": [t.model_dump(mode="json") for t in turns], "summary": summary_text}  # 写回
