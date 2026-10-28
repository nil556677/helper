"""历史投影：把「摘要 + 最近消息」还原成模型看到的历史 + token 裁剪。"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Sequence  # 输入类型
from typing import Any  # 任意值

from app.agent.contexts import tokens  # token 估算
from app.agent.contexts.budget import Budget  # 预算/裁剪目标

__all__ = ["project", "trim"]  # 对外导出


def project(summary_text: str, messages: Sequence[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:  # 投影
    """C1 只做形状：摘要原样 + 消息原样（C4 接压缩，C3 已接裁剪）。"""
    return str(summary_text or ""), [dict(item) for item in messages]  # 浅拷贝


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
