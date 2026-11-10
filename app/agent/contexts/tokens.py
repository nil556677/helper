"""上下文层唯一的 token 估算：复用 `chat_stats` 的 CJK 口径，全仓只此一处。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from app.services.chat_stats import estimate_tokens  # 唯一估算实现（界面占用比例同源）

__all__ = ["count_messages", "count_text"]  # 对外导出


def count_text(text: str) -> int:  # 一段文字
    """空串返回 0。"""
    return estimate_tokens(text)  # 交给唯一实现


def count_messages(messages: list[dict[str, Any]]) -> int:  # 一组消息
    """只算 content（role 不占 token）。"""
    total = 0  # 累计
    for item in messages:  # 逐条
        total += estimate_tokens(str(item.get("content") or ""))  # 累加正文
    return total  # 返回
