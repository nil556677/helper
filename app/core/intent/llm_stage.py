"""L3 LLM function calling：**本期只占位**。

``NullLLMStage`` 恒返回 ``None``，不发任何网络/LLM 请求；级联照常调用它。
将来实现真实分类器时替换 :data:`LLM_STAGE` 即可，路由与调用方不动。
"""

from __future__ import annotations

from typing import Protocol

from app.core.intent.schema import RouteDecision


class LLMIntentStage(Protocol):
    def classify(self, text: str) -> RouteDecision | None: ...


class NullLLMStage:
    """占位：永不命中、永不发请求。"""

    def classify(self, text: str) -> RouteDecision | None:
        del text
        return None


#: 当前实现（占位）。
LLM_STAGE: LLMIntentStage = NullLLMStage()


def set_stage(stage: LLMIntentStage) -> None:
    """换实现（测试 / 将来接 LLM 分类器）。"""
    global LLM_STAGE
    LLM_STAGE = stage
