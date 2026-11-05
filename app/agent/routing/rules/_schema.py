"""规则 schema：单条规则的校验与规范化（纯数据，无 IO）。"""

from __future__ import annotations  # 延迟求值注解

import re  # 正则
from dataclasses import dataclass, field  # 数据类
from typing import Any  # 任意值


@dataclass(frozen=True)
class RuleSpec:  # 一条规则
    """规则层的最小单元；`matches` 是纯函数。"""

    id: str  # 全局唯一
    priority: int  # 大优先
    exact: bool  # 仅 true 且唯一无歧义才允许 rule_hit
    route: str  # 固定路径名
    tools: tuple[str, ...] = ()  # 工具
    intent: str = "direct"  # 意图（当前恒为 direct）
    keywords: tuple[str, ...] = ()  # 关键词
    regexes: tuple[str, ...] = ()  # 正则
    ambiguous_examples: tuple[str, ...] = ()  # 易混输入（命中即强制降级）

    def matches(self, question: str) -> bool:  # 是否命中
        """关键词子串或正则命中即算；不访问任何外部状态。"""
        if any(word and word in question for word in self.keywords):  # 关键词
            return True
        return any(re.search(pattern, question) for pattern in self.regexes)  # 正则

    def is_ambiguous(self, question: str) -> bool:  # 是否易混
        """命中 ambiguous_examples 就强制降级到语义层。"""
        return any(sample and sample in question for sample in self.ambiguous_examples)


def parse_rule(raw: dict[str, Any], *, where: str = "") -> RuleSpec:  # 校验并规范化
    """缺字段/类型不对 → 直接报错（构建期 fail-fast）。"""
    if not isinstance(raw, dict):  # 必须是映射
        raise ValueError(f"{where}: 规则必须是 mapping")
    rule_id = str(raw.get("id") or "").strip()  # id
    if not rule_id:  # 必填
        raise ValueError(f"{where}: 规则缺少 id")
    match = raw.get("match") or {}  # 匹配块
    action = raw.get("action") or {}  # 动作块
    if not isinstance(match, dict) or not isinstance(action, dict):  # 形状
        raise ValueError(f"{where}: match/action 必须是 mapping")
    route = str(action.get("route") or "").strip()  # 路由
    if not route:  # 必须有落点
        raise ValueError(f"{where}: 规则 {rule_id} 缺少 action.route")
    regexes = tuple(str(x) for x in (match.get("regex") or ()))  # 正则
    for pattern in regexes:  # 预编译校验
        re.compile(pattern)
    return RuleSpec(  # 规范化
        id=rule_id,  # id
        priority=int(raw.get("priority") or 0),  # 优先级
        exact=bool(raw.get("exact", False)),  # 严格性
        route=route,  # 路径
        tools=tuple(str(x) for x in (action.get("tools") or ())),  # 工具
        intent=str(action.get("intent") or "direct"),  # 意图
        keywords=tuple(str(x) for x in (match.get("any") or ())),  # 关键词
        regexes=regexes,  # 正则
        ambiguous_examples=tuple(str(x) for x in (raw.get("ambiguous_examples") or ())),  # 易混
    )