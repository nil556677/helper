"""兜底子图结构化输出：PlanStep / Plan / ReplanDecision。

交给大模型的结构化模型：字段说明只写 `Field(description=...)`，每个字段有默认值。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Literal  # 字面量

from pydantic import BaseModel, Field  # 校验模型


class PlanStep(BaseModel):  # 一个计划步骤
    """原子可执行的一步，并标注归属专家。"""

    step: str = Field(default="", description="Atomic, actionable step. If it spans domains, split it.")  # 步骤
    agent: Literal["knowledge_agent", "general_agent", "unsupported"] = Field(  # 归属
        default="unsupported",  # 默认最保守
        description="knowledge_agent=multi-tool RAG (search/answer); general_agent=pure-LLM fallback for out-of-scope; unsupported=no existing expert handles this step",
    )  # 专家


class Plan(BaseModel):  # 完整计划
    """有序的原子步骤列表（1–7 步）。"""

    steps: list[PlanStep] = Field(default_factory=list, description="Ordered list of 1-7 atomic, actionable steps.")  # 步骤


class ReplanDecision(BaseModel):  # 重规划决策
    """在已有证据 + 候选上下文上判断继续/追加/终止。"""

    is_done: bool = Field(default=False, description="是否已完成")  # 完成
    final_response: str | None = Field(default=None, description="is_done=True 时的最终回答")  # 回答
    additional_steps: list[PlanStep] = Field(default_factory=list, description="追加的步骤（回 Execute）")  # 追加