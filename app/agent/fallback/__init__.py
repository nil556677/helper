"""LLM 兜底子图：Plan-and-Execute（Plan → Execute → Replan）+ 升级旁路。

语义 `ambiguous` 时进入；子图对父图是黑盒（`call_fallback_subgraph` wrapper 调用）。
"""

from __future__ import annotations  # 延迟求值注解

from app.agent.fallback.run import run_fallback  # 子图主循环
from app.agent.fallback.schema import Plan, PlanStep, ReplanDecision  # 结构化输出
from app.agent.fallback.state import FallbackState  # 子图状态

__all__ = ["FallbackState", "Plan", "PlanStep", "ReplanDecision", "run_fallback"]  # 导出