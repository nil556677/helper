"""复杂度判定：从"要走 agentic 的那批"里再拆出 simple / complex（零 LLM 强规则 + 中性档模型）。

原则（见《执行分层方案》§3）：
- 最省钱的强规则优先：复杂标记 / 长度阈值 / 深研模式 → complex；
- 无强信号的中性问题靠模型 `IntentDecision.complexity` 结构化输出，仅当置信度足够才采信 complex；
- 判定只影响"去 ReAct 还是 plan+execute"，不改变路由优先级。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.routing.retrieval_budget import _COMPLEX_MARKERS  # 复杂标记（复用单一源）
from app.agent.routing.triage import AGENTIC_MARKERS, MULTI_MARKERS  # 依赖链 / 多源标记（复用）

__all__ = ["DEFAULT_CONFIDENCE_THRESHOLD", "judge"]  # 导出

#: 中性档模型采信门槛。
DEFAULT_CONFIDENCE_THRESHOLD = 0.7  # 低于不采信模型 complex


def judge(  # 纯函数：按强规则 + 可选中性信号判定复杂度
    query: str,  # 规范化问题
    *,
    flags: dict[str, Any] | None = None,  # 硬约束（mode=deep_research 强 complex）
    intent_complexity: str = "simple",  # 模型结构化输出的复杂度（中性档）
    intent_confidence: float = 0.0,  # 模型置信度（≥阈值才采信 complex）
    length_threshold: int = 40,  # 强规则长度阈值
) -> str:  # "simple" / "complex"
    """强信号命中即 complex；否则中性模型高置信才有；否则 simple。"""
    text = str(query or "")  # 去空
    flags = flags or {}  # 硬约束
    # —— 强规则（零 LLM）——
    if str(flags.get("mode") or "") == "deep_research":  # 深研模式
        return "complex"  # 复杂（高强度检索）
    if any(word in text for word in _COMPLEX_MARKERS):  # 复杂/多跳标记
        return "complex"  # 复杂
    if any(word in text for word in AGENTIC_MARKERS):  # 依赖链/诊断标记
        return "complex"  # 复杂
    if any(word in text for word in MULTI_MARKERS):  # 多源/对比标记
        return "complex"  # 复杂
    if len(text) >= length_threshold:  # 长问题
        return "complex"  # 复杂（可能需多步）
    # —— 中性档：模型 high-confidence 采信 ——
    if intent_complexity == "complex" and intent_confidence >= DEFAULT_CONFIDENCE_THRESHOLD:  # 高置信才采信
        return "complex"  # 复杂
    return "simple"  # 简单
