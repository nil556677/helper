"""输出校验与安全（类 B 的 #11 输出侧）。

- **规则不重复实现**：L1 编号 / L2 支撑度 / L3 溯源全在 :mod:`app.rag.verify`；
- 本模块只补它不管的三件事：**判决口径**（含覆盖率阈值）、**清洗**（HTML / 危险链接）、
  **要不要修复**（给 repair 用的人话原因）。
"""

from __future__ import annotations

import re  # 清洗用的正则
from dataclasses import dataclass, field  # 报告
from typing import Any  # 任意值

from app.rag.verify import VerifyResult, verify  # 已有校验（L1/L2/L3）

#: 判定通过所需的最低引用覆盖率（调用处可覆盖）。
DEFAULT_MIN_COVERAGE = 0.5  # 一半以上的句子要有有效引用

#: 整段删除的危险标签（连同内容）。
_DANGEROUS_TAGS = ("script", "iframe", "object", "embed", "style", "form", "link", "meta")  # 危险

#: 允许保留的标签（渲染器支持的安全子集）。
_ALLOWED_TAGS = frozenset(  # 白名单
    {  # 集合开始
        "b", "i", "em", "strong", "u", "s", "del", "ins", "sub", "sup",  # 行内
        "code", "pre", "br", "p", "hr", "blockquote",  # 块
        "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",  # 列表与表格
        "h1", "h2", "h3", "h4", "h5", "h6",  # 标题
    }  # 集合结束
)  # 白名单结束

_TAG = re.compile(r"<\s*(/?)\s*([a-zA-Z][\w-]*)([^>]*)>")  # 任意标签
_EVENT_ATTR = re.compile(r"""\son\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)  # on* 事件属性
_LINK = re.compile(r"\]\(\s*<?([^)>\s]+)>?([^)]*)\)")  # markdown 链接目标
_UNSAFE_SCHEME = re.compile(r"^\s*(?:javascript|data|vbscript|file)\s*:", re.I)  # 危险协议


@dataclass(frozen=True)
class ValidationReport:  # 一次校验的报告
    """字段与 ``VerifyPayload`` 对齐，另加判决结果。"""

    text: str = ""  # 清洗后的正文
    valid: list[int] = field(default_factory=list)  # 有效引用
    invalid: list[int] = field(default_factory=list)  # 非法编号
    missing: list[int] = field(default_factory=list)  # 资料已失效
    unsupported: list[int] = field(default_factory=list)  # 没出处的句子
    coverage: float = 0.0  # 引用覆盖率
    passed: bool = False  # 是否判定通过
    min_coverage: float = DEFAULT_MIN_COVERAGE  # 本次用的阈值（报告里带着，便于解释）

    @property
    def needs_repair(self) -> bool:  # 要不要修
        """没通过就该修一次（有次数上限，由调用方控制）。"""
        return not self.passed  # 未通过


def sanitize_markdown(text: str) -> str:  # 输出安全清洗
    """剥危险标签 / on* 属性 / 危险协议；白名单外的标签去标签留内容。"""
    result = str(text or "")  # 原文
    for tag in _DANGEROUS_TAGS:  # 危险标签连同内容删掉
        result = re.sub(  # 成对
            rf"<\s*{tag}\b[^>]*>.*?<\s*/\s*{tag}\s*>",  # 开+闭
            "",  # 删掉
            result,  # 目标
            flags=re.S | re.I,  # 跨行/忽略大小写
        )  # 删除结束
        result = re.sub(rf"<\s*/?\s*{tag}\b[^>]*>", "", result, flags=re.I)  # 落单的也删
    result = _EVENT_ATTR.sub("", result)  # 去掉 onerror/onload 之类
    result = _TAG.sub(_keep_allowed, result)  # 白名单外去标签
    result = _LINK.sub(_safe_link, result)  # 危险链接换空锚
    return result  # 返回


def _keep_allowed(match: re.Match[str]) -> str:  # 白名单标签判断
    """允许的保留原样，其余整段丢掉（内容留着）。"""
    name = match.group(2).lower()  # 标签名
    return match.group(0) if name in _ALLOWED_TAGS else ""  # 保留或去掉


def _safe_link(match: re.Match[str]) -> str:  # 链接协议判断
    """只放行 http/https/mailto/相对地址；javascript:/data: 等换成 #。"""
    url = match.group(1)  # 目标
    if _UNSAFE_SCHEME.match(url):  # 危险协议
        return "](#)"  # 换成空锚
    return match.group(0)  # 原样


def validate_answer(  # 总校验入口
    text: str,  # 模型正文
    evidence: list[dict[str, Any]],  # 证据
    *,
    lookup: Any = None,  # L3 查询（None 跳过溯源）
    min_coverage: float = DEFAULT_MIN_COVERAGE,  # 覆盖率阈值
    l2: bool = True,  # 是否做支撑度
) -> ValidationReport:  # 报告
    """清洗 → L1/L3/L2 → 按「有效引用 + 无失效 + 覆盖率 + 无未支撑」判决。"""
    cleaned = sanitize_markdown(text)  # 先清洗（防注入/危险链接）
    result: VerifyResult = verify(cleaned, evidence, lookup=lookup, l2=l2)  # 复用已有校验
    passed = (  # 判决口径
        bool(result.valid)  # 至少一个有效引用
        and not result.missing  # 没有失效引用
        and result.coverage >= min_coverage  # 覆盖率达标
        and not result.unsupported  # 没有无出处的句子
    )  # 判决结束
    return ValidationReport(  # 报告
        text=result.text,  # 清理后正文
        valid=list(result.valid),  # 有效
        invalid=list(result.invalid),  # 非法
        missing=list(result.missing),  # 失效
        unsupported=list(result.unsupported),  # 未支撑
        coverage=result.coverage,  # 覆盖率
        passed=passed,  # 判决
        min_coverage=min_coverage,  # 阈值
    )  # 报告结束


def report_text(report: ValidationReport) -> str:  # 报告 → 人话
    """给修复 prompt 用：说清哪里不对，别让模型猜。"""
    parts: list[str] = []  # 原因
    if not report.valid:  # 一个有效引用都没有
        parts.append("没有任何有效引用")  # 记
    if report.invalid:  # 编号越界
        parts.append(f"这些编号不存在：{report.invalid}")  # 记
    if report.missing:  # 引用的资料没了
        parts.append(f"这些引用的资料已不可用：{report.missing}")  # 记
    if report.unsupported:  # 有句子没出处
        parts.append(f"这些句子没有出处：第 {report.unsupported} 句")  # 记
    if report.coverage < report.min_coverage:  # 覆盖率不够
        parts.append(f"引用覆盖率 {report.coverage:.0%} 低于要求 {report.min_coverage:.0%}")  # 记
    if not parts:  # 兜底
        parts.append("答案为空或格式不合法")  # 记
    return "；".join(parts) + "。只修正引用与出处，不新增事实。"  # 返回
