"""兜底子图 State：与父图隔离，只暴露契约键。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, TypedDict  # 状态类型


class PlanStepModel(TypedDict):  # 单步（序列化形态）
    step: str  # 原子步骤
    agent: str  # query_agent / action_agent / sensitive_agent / unsupported


class FallbackState(TypedDict, total=False):  # 兜底子图状态
    query: str  # 父图注入
    semantic_candidates: list[dict[str, Any]]  # 语义层候选（Planner/Replanner 参考）
    thread_id: str  # 与父图共享（检查点/恢复）
    plan: list[PlanStepModel]  # 剩余待执行步骤
    past_steps: list[tuple[str, str]]  # [(step, result), ...]
    unsupported_steps: list[PlanStepModel]  # 无对应专家的步骤（显式暴露）
    replan_count: int  # 重规划次数
    final_response: str  # 最终回答
    is_done: bool  # 是否完成
    error_kind: str  # 终止原因（degrade / unsupported / stop_loss）