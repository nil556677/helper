"""动态层：V1 工具结果（含检索）、V2 用户上传资料、V3 当前问题。"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Iterable  # 列表输入
from typing import Any  # 任意值

from app.agent.contexts import blocks  # 块工具

__all__ = ["attachments", "question", "tool_results"]  # 对外导出


def tool_results(items: Iterable[dict[str, Any]]) -> str:  # V1
    """一条一个 ``<tool_result>``，外层包 ``<tool_results>``；同类合并成一块。"""
    parts = [  # 逐条成块
        blocks.block(  # 单条工具结果
            "tool_result",  # 标签
            str(item.get("body") or ""),  # 正文
            call_id=str(item.get("call_id") or ""),  # 调用 id
            name=str(item.get("name") or ""),  # 工具名
            status=str(item.get("status") or "ok"),  # 状态
        )
        for item in items  # 遍历
    ]  # 列表结束
    return blocks.container("tool_results", blocks.join_blocks(*parts))  # 外层容器


def attachments(items: Iterable[dict[str, Any]]) -> str:  # V2
    """一条一个 ``<attachment name=...>``；空文本跳过（图片暂不解析）。"""
    parts = [  # 逐条成块
        blocks.block("attachment", str(item.get("text") or ""), name=str(item.get("name") or "附件"))  # 单条
        for item in items  # 遍历
        if str(item.get("text") or "").strip()  # 只收有文本的
    ]  # 列表结束
    return blocks.container("attachments", blocks.join_blocks(*parts))  # 外层容器


def question(text: str) -> str:  # V3
    """本轮问题；空则不出块。"""
    return blocks.block("question", text)  # 成块
