"""L5 生成与校验：引用角标校验 + 覆盖率 + PII 输出脱敏（零 LLM）。"""

from __future__ import annotations  # 延迟求值注解

import re  # 正则

from app.agent.contracts.triage import VerifyResult  # 契约
from app.rag.answer import validate_citations  # 复用角标校验

__all__ = ["mask_pii", "strip_invalid", "verify"]  # 导出

#: 引用角标。
_CITE = re.compile(r"\[(\d+)\]")  # 角标

#: PII 脱敏规则（输出前兜底）。
_PII_RULES: tuple[tuple[re.Pattern[str], str], ...] = (  # 规则
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[邮箱已脱敏]"),  # 邮箱
    (re.compile(r"\b1[3-9]\d{9}\b"), "[手机号已脱敏]"),  # 手机号
    (re.compile(r"\b\d{16,19}\b"), "[卡号已脱敏]"),  # 卡号
)  # 规则结束


def mask_pii(text: str) -> str:  # 输出脱敏
    """按规则替换；只影响输出，不改业务数据。"""
    masked = str(text or "")  # 原文
    for pattern, replacement in _PII_RULES:  # 逐规则
        masked = pattern.sub(replacement, masked)  # 替换
    return masked  # 返回


def strip_invalid(text: str, max_n: int) -> str:  # 删越界角标
    """保留合法 [n]，删掉 1..max_n 之外的角标。"""

    def _keep(match: re.Match[str]) -> str:  # 判定
        number = int(match.group(1))  # 序号
        return match.group(0) if 1 <= number <= max_n else ""  # 保留或删

    return _CITE.sub(_keep, str(text or ""))  # 替换


def verify(answer: str, evidence: list[dict]) -> VerifyResult:  # 校验
    """返回结构化的引用校验结果；passed 需有正文且无越界角标且有覆盖。"""
    text = str(answer or "")  # 答案
    max_n = len(evidence or [])  # 证据数
    _clean, valid = validate_citations(text, evidence or [])  # 合法引用
    cited = {int(raw) for raw in _CITE.findall(text)}  # 全部角标
    invalid = sorted(n for n in cited if not (1 <= n <= max_n))  # 越界
    missing = sorted(n for n in range(1, max_n + 1) if n not in cited)  # 未引用
    coverage = round(len(valid) / max_n, 4) if max_n else 0.0  # 覆盖率
    reasons: list[str] = []  # 原因
    if not text.strip():  # 空答案
        reasons.append("empty_answer")  # 空
    if invalid:  # 越界
        reasons.append("invalid_citation")  # 越界
    if max_n and coverage == 0.0:  # 无引用
        reasons.append("no_citation")  # 无引用
    passed = bool(text.strip()) and not invalid and (coverage > 0.0 if max_n else True)  # 判定
    return VerifyResult(  # 结果
        valid=valid,  # 合法
        invalid=invalid,  # 越界
        missing=missing,  # 未引
        unsupported=[],  # 占位（模型断言级校验后续接）
        coverage=coverage,  # 覆盖率
        passed=passed,  # 通过
        reasons=reasons,  # 原因
    )