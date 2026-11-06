"""L1 实体补全：正则抽显式实体 + 从历史继承（**不做指代改写**）。"""

from __future__ import annotations  # 延迟求值注解

import re  # 正则
from typing import Any  # 任意值

from app.agent.contracts.triage import EntityBundle  # 契约

#: 实体抽取规则：键 → 正则（取第 1 组）。
_ENTITY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (  # 规则表
    ("order_id", re.compile(r"(?:订单|order)[^\dA-Za-z]{0,6}([A-Za-z0-9]{4,})", re.I)),  # 订单号
    ("amount", re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|rmb)", re.I)),  # 金额
    ("date", re.compile(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})")),  # 日期
)  # 规则表结束

__all__ = ["extract_entities", "inherit_from_turns"]  # 导出


def extract_entities(query: str) -> dict[str, Any]:  # 显式实体
    """按规则表顺序抽取；同键取首个命中。"""
    found: dict[str, Any] = {}  # 结果
    for key, pattern in _ENTITY_RULES:  # 逐规则
        match = pattern.search(str(query or ""))  # 搜索
        if match:  # 命中
            found[key] = match.group(1)  # 取第 1 组
    return found  # 返回


def inherit_from_turns(turns: list[dict[str, Any]]) -> dict[str, Any]:  # 历史实体继承
    """从最近 Turn 的结构化 entities 字段补；没有结构化字段就不补（不猜）。"""
    for turn in reversed(turns or []):  # 从近到远
        entities = turn.get("entities") if isinstance(turn, dict) else None  # 结构化字段
        if isinstance(entities, dict) and entities:  # 有就返回
            return dict(entities)  # 复制
    return {}  # 没有
