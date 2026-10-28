"""固定层：S1 全局、S2 智能体规则、S3 静态环境。"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Mapping  # 环境项类型

from app.agent.contexts import (
    blocks,  # 块工具
    prompts,  # 模板注册表
)
from app.agent.contexts.prompts import SYSTEM_BASE_UID  # S1 的 uid

__all__ = ["agent_rules", "static_env", "system_base"]  # 对外导出


def system_base() -> str:  # S1
    """全局身份 + 安全底线。"""
    return prompts.get(SYSTEM_BASE_UID).render()  # 渲染


def agent_rules(name: str) -> str:  # S2
    """按 agent 取规则模板（不存在直接 KeyError）。"""
    return prompts.get(name).render()  # 渲染


def static_env(items: Mapping[str, str]) -> str:  # S3
    """每行 ``key: value``；空项/空值跳过，全空则不出块。"""
    lines = [f"{key}: {value}" for key, value in items.items() if str(value).strip()]  # 有效行
    return blocks.block("env", "\n".join(lines))  # 成块（空则空串）
