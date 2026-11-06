"""HITL 恢复入口：用同一 `thread_id` 的 `Command(resume=...)` 继续被暂停的运行。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from langgraph.types import Command  # 恢复命令

__all__ = ["interrupt_payload", "interrupted", "resume"]  # 导出


def interrupted(result: dict[str, Any] | None) -> bool:  # 是否暂停
    """图返回含 `__interrupt__` 即暂停。"""
    return isinstance(result, dict) and "__interrupt__" in result  # 判断


def interrupt_payload(result: dict[str, Any] | None) -> dict[str, Any] | None:  # 取待办负载
    """返回第一个中断的 value（前端渲染用）；无则 None。"""
    if not interrupted(result):  # 没暂停
        return None  # 空
    items = result["__interrupt__"]  # 中断列表
    return getattr(items[0], "value", None) if items else None  # 第一个负载


async def resume(graph: Any, thread_id: str, value: Any) -> dict[str, Any]:  # 恢复
    """同 thread_id 恢复；value 为用户选择/批准结果。"""
    return await graph.ainvoke(Command(resume=value), {"configurable": {"thread_id": str(thread_id)}})  # 继续