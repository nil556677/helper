"""后台会话压缩协调器：每会话单飞 + 追加分段 + 失败兜底（异步，不打主线）。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 后台任务
from typing import Any  # 任意值

__all__ = ["maybe_enqueue", "running"]  # 导出

#: 每会话正在跑的压缩任务（单飞锁：防同一会话两段同时压）。
_running: dict[str, asyncio.Task] = {}


def running(conversation_id: str) -> bool:  # 该会话是否有压缩在跑
    """查询用；已在跑时不再入队（防重叠）。"""
    return str(conversation_id) in _running  # 判


def maybe_enqueue(conversation_id: str) -> bool:  # 入队后台压缩
    """每会话单飞：已在跑则跳过；否则起一个后台任务压缩最老一段。"""
    key = str(conversation_id)  # 会话键
    if key in _running:  # 已有在跑
        return False  # 不重复
    task = asyncio.create_task(_run(key))  # 起后台任务
    _running[key] = task  # 占位
    return True  # 已入队


async def _run(conversation_id: str) -> None:  # 执行一次压缩
    """压最老一段并追加分段；失败兜底（不动 until，下轮自然重试）。"""
    try:  # 任何失败都不影响主线
        await _compact_once(conversation_id)  # 真正压缩
    finally:  # 一定释放单飞位
        _running.pop(conversation_id, None)  # 移除


async def _compact_once(conversation_id: str) -> None:  # 一次压缩到段
    """读最近消息 → 拆最老一段 → LLM 摘要 → 追加 summary_segment。"""
    from app.agent.contexts.budget import Budget  # 预算
    from app.agent.contexts.compact import plan  # 拆分
    from app.agent.contexts.history import messages_from, turns_from_rows  # 投影
    from app.agent.contexts.summarize import build_summarizer  # 摘要器
    from app.agent.contexts.tokens import count_messages  # 估算
    from app.services import conversations  # 会话

    budget = Budget()  # 预算（window=1M 推导）
    rows = conversations.load_messages(conversation_id)  # 读全部消息
    head = conversations.load_summary(conversation_id)  # 最后段边界
    since = int(head.get("until_message_id") or 0)  # 覆盖终点
    turns = turns_from_rows(since, rows)  # 摘要之后成轮
    messages = messages_from(turns, with_ids=True)  # 消息投影（带 id）
    if not messages:  # 没消息可压
        return  # 跳过
    if not budget.should_compact(  # 触发判定（预算 + 滞回 + 次数）
        count_messages(messages),  # 历史 token
        turns_since_last=len(turns),  # 距上次约几轮
        compactions=conversations.segment_count(conversation_id),  # 已有段数
    ):  # 判定结束
        return  # 不压
    compactor = build_summarizer(None)  # 摘要器（默认 llm；不可用返回 None）
    if compactor is None:  # 摘要模型不可用
        return  # 退回只 trim（主线已做过）
    _keep, oldest = plan(messages, budget)  # 拆 (保留区, 最老一段)
    if not oldest:  # 无最老段
        return  # 跳过
    new_summary = (await compactor(oldest, str(head.get("summary") or "")) or "").strip()  # 生成
    if not new_summary:  # 空摘要
        return  # 拒绝
    if count_messages(oldest) <= len(new_summary):  # 没变短
        return  # 拒绝（不劣化）
    refs = [int(item.get("id") or 0) for item in oldest if item.get("id")]  # source_refs
    if not refs:  # 无 id（防御）
        return  # 跳过
    conversations.append_segment(  # 追加一段（永不重写旧段）
        conversation_id,  # 会话
        start_id=min(refs),  # 起始 id
        end_id=max(refs),  # 终点 id（= until）
        summary=new_summary,  # 摘要
        source_refs=refs,  # 可回放引用
    )  # 结束