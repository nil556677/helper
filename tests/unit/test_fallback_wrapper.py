"""兜底子图工厂与 wrapper：模型不可用优雅降级。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 跑 async 降级

from app.agent.fallback.factory import make_planner, make_replanner  # 工厂
from app.agent.fallback.wrapper import call_fallback_subgraph  # wrapper


def test_make_planner_degrades_without_model() -> None:  # 无模型 → 全标 unsupported
    plan = asyncio.run(make_planner(None)("随便一个问题", []))  # 规划（async 降级）
    assert plan.steps and plan.steps[0].agent == "unsupported"  # 降级


def test_make_replanner_degrades_without_model() -> None:  # 无模型 → 直接完成
    decision = asyncio.run(make_replanner(None)({"past_steps": [], "plan": []}))  # 重规划（async 降级）
    assert decision.is_done is True  # 结束


async def test_wrapper_degrades_without_model() -> None:  # wrapper 无模型不炸、给降级话术
    out = await call_fallback_subgraph(  # 调 wrapper
        {"question": "q", "semantic_recall": {"decision": "ambiguous", "candidates": []}, "conversation_id": "c"}  # 输入
    )  # 结束
    assert out["fallback_used"] is True  # 标了
    assert out["answer"]  # 有降级话术（不空）