"""LLM 兜底：单一 ReAct agent（可调 天气/时间/本地资料检索 3 工具）。

替代原 Plan-and-Execute 子图：不再 planner/replanner 多步规划，直接把不确定请求
（代指/含糊/未命中/多轮）交给一个 ReAct agent + 完整记忆处理。
"""

from __future__ import annotations  # 延迟求值注解

from app.agent.fallback.agent import run_react  # 单一 ReAct 兜底
from app.agent.fallback.wrapper import call_fallback_subgraph  # 父图 wrapper

__all__ = ["run_react", "call_fallback_subgraph"]  # 导出