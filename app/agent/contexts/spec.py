"""每个 agent 的上下文声明：S1/S2 模板 uid、S3 静态环境、预算覆盖。

**写死在代码里**（不读配置）：换 agent 就是换一份 spec；采样参数仍走 B 的“调用处声明”。
模板 uid 直接用 `prompts` 导出的常量，避免手敲长路径，也避免短名。
"""

from __future__ import annotations  # 类型注解延迟求值

from dataclasses import dataclass, field  # 数据类

from app.agent.contexts.prompts import ANSWER_RULES_UID, SYSTEM_BASE_UID  # 模板 uid 常量

__all__ = ["AgentContextSpec"]  # 对外导出


@dataclass(frozen=True)
class AgentContextSpec:  # 一个 agent 的上下文声明
    """只描述“这个 agent 用哪些块”，不写块内容。"""

    agent: str  # 智能体名（如 answer）
    system_prompt: str = SYSTEM_BASE_UID  # S1 全局模板 uid
    rules_prompt: str = ANSWER_RULES_UID  # S2 本 agent 规则模板 uid
    static_env: dict[str, str] = field(default_factory=dict)  # S3 环境项（写死，可为空）
    history_budget: int | None = None  # 预算覆盖（None = 用 Budget 默认）
