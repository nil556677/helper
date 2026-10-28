"""压缩引擎：把最老一段历史并入滚动摘要（模糊远期记忆）。

设计（定稿）：
- 复用 `history.trim` 的边界判定（保留区内 ≤ `trim_target`），旧消息进摘要；
- **拒绝不变小/变空的摘要**（比旧摘要+旧消息还长就放弃，退回 trim）；
- 原文不删，只写 `conversation_summaries` 投影；可回放。
"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Awaitable, Callable, Sequence  # 类型
from dataclasses import dataclass  # 结果容器
from typing import Any  # 任意值

from app.agent.contexts import tokens  # token 估算
from app.agent.contexts.budget import Budget  # 预算
from app.agent.contexts.history import trim  # 复用边界判定

__all__ = ["SummaryResult", "compact", "plan"]  # 对外导出


@dataclass(frozen=True)
class SummaryResult:  # 压缩结果
    """新摘要 + 覆盖到哪条 + 计数。"""

    summary: str  # 新滚动摘要
    until_message_id: int  # 覆盖到的最后一条消息 id
    compacted_messages: int  # 并入摘要的消息数
    saved_tokens: int  # 净省 token（消息→摘要）


Compactor = Callable[[Sequence[dict[str, Any]], str], Awaitable[str]]  # (旧消息, 旧摘要) -> 新摘要


def plan(  # 选出“该进摘要的最老一段”
    messages: Sequence[dict[str, Any]],  # 按 seq 升序（最老在前）
    budget: Budget | None = None,  # 预算
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:  # (将被保留的, 将被并入摘要的)
    """返回 (keep, oldest)；keep 为最近那批（≤ trim_target），oldest 为其余。"""
    used = budget or Budget()  # 预算
    if tokens.count_messages(messages) <= used.trim_target:  # 没超
        return list(messages), []  # 全保留、无事可压
    keep, _, _ = trim(messages, used)  # 复用边界的保留区
    n_keep = len(keep)  # 保留条数
    if n_keep == 0:  # 无可保留（防御）
        return [], list(messages)  # 全压（异常，上游会拒摘要）
    return keep, list(messages[: len(messages) - n_keep])  # 拆分


async def compact(  # 执行一次滚动压缩
    messages: Sequence[dict[str, Any]],  # 全部历史（按 seq 升序）
    compactor: Compactor,  # 摘要生成器（真实走网关；测试可注假）
    *,
    summary_text: str = "",  # 旧摘要
    budget: Budget | None = None,  # 预算
) -> SummaryResult | None:  # 结果或 None（无事可做 / 摘要被拒）
    """压缩最老一段；摘要未变小时拒绝并返回 None（调用方退回 trim）。"""
    _keep, oldest = plan(messages, budget)  # 拆分
    if not oldest:  # 没有可压缩的
        return None  # 跳过
    new_summary = await compactor(oldest, summary_text)  # 生成
    new_summary = str(new_summary or "").strip()  # 清
    old_cost = tokens.count_text(summary_text) + tokens.count_messages(oldest)  # 旧开销
    new_cost = tokens.count_text(new_summary)  # 新开销
    if not new_summary or new_cost > old_cost:  # 空或没变短
        return None  # 拒绝
    until_id = int(oldest[-1].get("id") or oldest[-1].get("seq") or 0)  # 最后一条老消息
    saved = tokens.count_messages(oldest) - new_cost  # 净省
    return SummaryResult(  # 返回
        summary=new_summary,  # 新摘要
        until_message_id=until_id,  # 覆盖终点
        compacted_messages=len(oldest),  # 并入条数
        saved_tokens=max(saved, 0),  # 省 token（不小于 0）
    )
