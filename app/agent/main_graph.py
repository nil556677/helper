"""总图骨架：用户输入 → 三层路由 → react / chitchat / clarify → 输出。

当前实现复用现有父图（agent.build_route_graph）：路由→dispatch/llm_fallback(自定义ReAct)/general/clarify。
本文件作为"总图"的单一入口别名，供后续独立组装/测试引用。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from app.agent.agent import build_route_graph  # 现有父图（即总图）

__all__ = ["build_main_graph"]  # 导出


def build_main_graph(*, checkpointer: Any = None) -> Any:  # 总图
    """返回总图（当前 = 路由父图；ReAct 为其中 llm_fallback 节点）。"""
    return build_route_graph(checkpointer=checkpointer)  # 复用