"""依赖链判定（LLM 兜底，**极窄条件**）。

只有当"规则词表（AGENTIC_MARKERS/MULTI_MARKERS）未命中、且语义层 ambiguous"时，
才调用一次轻量分类，判断问题是否需要多步依赖（走 agentic）。
LLM 只做分类，不做执行规划；失败一律返回 False（退回规则结果）。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from pydantic import BaseModel, Field  # 结构化输出

__all__ = ["DependencyDecision", "needs_agentic"]  # 导出


class DependencyDecision(BaseModel):  # 依赖链二分类
    """只回答"要不要多步"。"""

    agentic: bool = Field(default=False, description="是否需要多步依赖/推理；是则 True")  # 结论


async def needs_agentic(query: str, model: Any | None) -> bool:  # 依赖链判定
    """有模型才判；无模型/失败返回 False（保守，不退化成多步）。"""
    if model is None:  # 无模型
        return False  # 保守 False
    try:  # 调模型（结构化二分类）
        runner = model.with_structured_output(DependencyDecision)  # 结构化
        out = await runner.ainvoke(  # 请求
            [
                ("system", "判断用户问题是否需要多步依赖/推理（对比、因果、多条件、组合、逐步）。只输出布尔结论。"),  # 指令
                ("human", str(query or "")),  # 问题
            ]
        )  # 结束
        return bool(getattr(out, "agentic", False))  # 结论
    except Exception:  # 任何失败
        return False  # 保守