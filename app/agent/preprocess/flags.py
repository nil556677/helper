"""L1 Flags 注入：前端/会话/偏好 → 硬约束标志（L3 短路读，模型不可推翻）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

__all__ = ["collect_flags", "FLAG_KEYS"]  # 导出

#: 受支持的硬约束 flag（其余忽略，防注入噪声）。
FLAG_KEYS: frozenset[str] = frozenset({  # 允许的键
    "mode",  # answer_only / deep_research
    "format",  # table / json / markdown
    "source_hint",  # 收窄检索源
    "no_tools",  # 强制 general_agent
    "no_rewrite",  # 禁止改写
})  # 集合结束


def collect_flags(source: dict[str, Any] | None) -> dict[str, Any]:  # 收集
    """只保留白名单键；值是原样透传。"""
    return {key: value for key, value in (source or {}).items() if key in FLAG_KEYS}  # 白名单过滤
