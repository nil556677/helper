"""固定变化层：F1 事实档案、F2 压缩摘要、F3 最近 N 轮。"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Iterable  # 事实列表
from typing import Any  # 任意值

from app.agent.contexts import blocks  # 块工具

__all__ = ["facts", "recent", "summary"]  # 对外导出


def facts(items: Iterable[str]) -> str:  # F1
    """一条一行；空条目不渲染，全空不出块。"""
    lines = [f"- {str(item).strip()}" for item in items if str(item).strip()]  # 有效条目
    return blocks.block("user_memory", "\n".join(lines))  # 成块


def summary(text: str) -> str:  # F2
    """压缩摘要；空则不出块。"""
    return blocks.block("compacted-summary", text)  # 成块


def recent(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:  # F3
    """原样返回（裁剪/压缩在 C3/C4；C1 只做形状）。"""
    return [dict(item) for item in messages]  # 浅拷贝，避免外部改动
