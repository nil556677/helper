"""`llm_fallback` 节点包：父图把粒度交给兜底子图的黑盒入口。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.fallback.wrapper import call_fallback_subgraph  # 子图 wrapper
from app.agent.llm.provider import get_chat_model  # 统一模型入口


async def llm_fallback_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """把 ambiguous/agentic 请求交给兜底子图（黑盒），结果写回父图。"""
    del config  # 不用图配置
    return await call_fallback_subgraph(state, model=get_chat_model())  # 用统一提供者的模型跑子图


__all__ = ["llm_fallback_node"]  # 导出