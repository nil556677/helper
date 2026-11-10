"""历史投影：把「摘要 + 最近轮次」还原成模型看到的历史 + token 裁剪。"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Sequence  # 输入类型
from typing import Any  # 任意值

from app.agent.contexts import tokens  # token 估算
from app.agent.contexts.budget import Budget  # 预算/裁剪目标
from app.agent.contracts import Turn, TurnStatus, only_done  # 轮次契约

__all__ = ["messages_from", "trim", "turns_from_rows"]  # 对外导出


def turns_from_rows(since: int, rows: Sequence[dict[str, Any]], *, limit: int = 200) -> list[Turn]:  # 行 -> 轮次
    """按「一问一答」成对收集覆盖点之后的历史；空回答标 ``unanswered``。"""
    turns: list[Turn] = []  # 结果
    pending: tuple[int, str] | None = None  # 暂存等待配对的 user
    for row in list(rows)[-limit:]:  # 最近一批
        ident = int(row.get("id") or 0)  # 消息 id
        if ident <= since:  # 已被摘要覆盖
            continue  # 跳过
        role = str(row.get("role") or "")  # 角色
        content = str(row.get("content") or "")  # 内容
        if role == "user":  # 用户问题
            pending = (ident, content) if content else None  # 暂存（空问题丢弃）
        elif role == "assistant":  # 助手回答
            if pending is not None:  # 有配对问题
                user_id, user_text = pending  # 取出
                answered = bool(content.strip())  # 是否真有回答
                turns.append(  # 组一轮
                    Turn(  # 轮次
                        turn_id=str(user_id),  # 轮次 id = user 消息 id
                        status=TurnStatus.DONE if answered else TurnStatus.UNANSWERED,  # 状态
                        user=user_text,  # 问题
                        assistant=content if answered else None,  # 回答（空则 None）
                    )  # 轮次结束
                )  # 追加结束
            pending = None  # 无论成败都清空
    return turns  # 返回（结尾未配对的 user 自动丢弃）


def messages_from(turns: Sequence[Turn], *, with_ids: bool = False) -> list[dict[str, Any]]:  # 轮次 -> 消息
    """把 done 轮物化成模型消息；``with_ids`` 时带上边界用的 id。"""
    out: list[dict[str, Any]] = []  # 结果
    for turn in only_done(turns):  # 只喂 done
        if with_ids:  # 需要 id
            out.append({"role": "user", "content": turn.user, "id": int(turn.turn_id)})  # 问题
            out.append({"role": "assistant", "content": turn.assistant or "", "id": int(turn.turn_id)})  # 回答
        else:  # 不需要 id
            out.append({"role": "user", "content": turn.user})  # 问题
            out.append({"role": "assistant", "content": turn.assistant or ""})  # 回答
    return out  # 返回


def trim(  # 按 token 裁剪历史
    messages: Sequence[dict[str, Any]],  # 消息
    budget: Budget | None = None,  # 预算（裁剪目标 = budget.trim_target）
) -> tuple[list[dict[str, Any]], int, int]:  # (保留, 丢条数, 省 token)
    """从最老开始丢、保最近；**不拆半条消息**；至少保留一条。"""
    used = budget or Budget()  # 生效预算
    total = tokens.count_messages(messages)  # 现有总量
    if total <= used.trim_target:  # 没超目标
        return list(messages), 0, 0  # 原样
    kept_rev: list[dict[str, Any]] = []  # 倒序累积
    acc = 0  # 已保留 token
    for item in reversed(list(messages)):  # 从最新往前
        cost = tokens.count_text(str(item.get("content") or ""))  # 本条
        if acc + cost > used.trim_target and kept_rev:  # 再收就超且已有一条
            break  # 停止
        kept_rev.append(item)  # 保留
        acc += cost  # 累加
    kept = list(reversed(kept_rev))  # 还原正序
    saved = total - tokens.count_messages(kept)  # 释放 token
    return kept, len(messages) - len(kept), saved  # 返回
