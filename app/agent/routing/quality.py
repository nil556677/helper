"""L4a 质量信号：判断快捷检索结果是否够好，不够好带原因升级 agentic。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.routing.triage import AGENTIC_MARKERS, MULTI_MARKERS  # 复用信号词

__all__ = ["assess"]  # 导出

#: 最少有效命中数。
MIN_RESULTS = 3  # 少于就升级

#: 平均分基准（低于就 coverage_low）。
COVERAGE_BASELINE = 0.30  # 基准

#: 指向性表述（命中说明资料不完整）。
_OPEN_MARKERS = ("参见", "前提是", "详见", "见附件")  # 表述


def assess(evidence: list[dict[str, Any]], query: str) -> tuple[bool, str]:  # 评估
    """返回 (是否升级, 原因)；no 时为 ("", "")。"""
    text = str(query or "")  # 原问题
    if len(evidence or []) < MIN_RESULTS:  # 结果太少
        return True, "insufficient_results"  # 升级
    scores = [float(item.get("score") or 0.0) for item in evidence if isinstance(item, dict)]  # 分数
    if scores and (sum(scores) / len(scores)) < COVERAGE_BASELINE:  # 平均分低
        return True, "coverage_low"  # 升级
    sources = {str(item.get("source_name") or "") for item in evidence if isinstance(item, dict)}  # 来源
    if len(sources) == 1 and any(word in text for word in MULTI_MARKERS):  # 单源但问多源
        return True, "single_source"  # 升级
    if any(word in str(item.get("text") or "") for item in evidence if isinstance(item, dict) for word in _OPEN_MARKERS):  # 指向性
        return True, "open_question"  # 升级
    if any(word in text for word in AGENTIC_MARKERS):  # 需要推理但只有事实片段
        return True, "needs_reasoning"  # 升级
    return False, ""  # 达标