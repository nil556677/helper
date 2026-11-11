"""检索置信度：用于 full 档决定"是否值得再烧一次 LLM 多路召回"。纯规则。"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

__all__ = ["confidence_of", "should_enhance"]  # 导出

#: 低于该置信度才触发 full 档增强（可调）。
ENHANCE_THRESHOLD = 0.6  # 阈值


def confidence_of(gate: dict[str, Any] | None) -> float:  # 置信度 0..1
    """门限判决 + top1 分数 → 粗略置信度（越高越可信）。"""
    data = gate or {}  # 判决
    decision = str(data.get("decision") or "").lower()  # pass/weak/reject
    base = {"pass": 0.95, "weak": 0.5, "reject": 0.2}.get(decision, 0.35)  # 判决基线
    try:  # top1 可能缺失/坏值
        top1 = float(data.get("top1") or 0.0)  # 最高分
    except (TypeError, ValueError):  # 坏值
        top1 = 0.0  # 归零
    top1 = max(0.0, min(1.0, top1))  # 夹到 0..1
    return round(min(1.0, base * 0.6 + top1 * 0.4), 4)  # 加权


def should_enhance(gate: dict[str, Any] | None, threshold: float = ENHANCE_THRESHOLD) -> bool:  # 是否触发增强
    """**只有「已召回到东西但融合/精排弱」才值得烧 LLM 多路；reject/无证据绝不触发。**

    改写(HyDE/PAR)改善的是「有候选但质量弱」的召回，对「压根没有可用证据」毫无帮助——
    改写变体造不出本来就不存在的数据。所以拒答/reject（no_evidence/low_score/qa_low）一律不触发，
    避免白烧一次 generate；只有 weak 且低置信（fusion 弱、caught 但置信不足）才值得增强。
    """
    data = dict(gate or {})  # 判决
    decision = str(data.get("decision") or "").lower()  # pass/weak/reject
    if decision == "reject":  # 已判定不可答 → 增强无益
        return False  # 不触发（改写/HyDE 造不出数据）
    return confidence_of(gate) < float(threshold)  # 其余档：低置信才增强