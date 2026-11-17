"""Skill 清单模型：一个能力包的声明式 schema（pydantic + 默认值）。

字段给模型或跨进程用的用 `Field(description=...)`；配置型走行尾中文注释。
"""

from __future__ import annotations  # 延迟值注解

from typing import Literal  # 类型

from pydantic import BaseModel, Field  # 校验

__all__ = ["CostBudget", "SkillManifest", "SkillToolRole"]  # 导出

#: 副作用等级（复用 tool_spec 语义）。
_SIDE_EFFECT = Literal["none", "reversible", "irreversible"]  # 等级
#: 工具角色。
_ROLE = Literal["primary", "support", "optional"]  # 角色
#: 审批策略。
_APPROVAL = Literal["none", "auto", "manual"]  # 审批


class SkillToolRole(BaseModel):  # 一个工具角色
    name: str = Field(default="", description="工具名（必须已在 tool_catalog）")  # 工具名
    role: _ROLE = Field(default="support", description="主/支撑/可选：primary 必须被调用才满足能力")  # 角色
    approval: _APPROVAL = Field(default="none", description="覆盖该工具的审批策略（默认随工具声明）")  # 审批


class CostBudget(BaseModel):  # 成本预算（防"贵而没用"）
    max_tokens: int = Field(default=2000, description="单次调用的模型 token 上限")  # token
    max_ms: int = Field(default=5000, description="单次调用耗时上限（毫秒）")  # 耗时


class SkillManifest(BaseModel):  # 能力包清单
    id: str = Field(default="", description="skill id（命名空间内唯一）")  # id
    namespace: str = Field(default="default", description="命名空间（归并同类）")  # 命名空间
    version: int = Field(default=1, description="语义化版本（破坏性变更 +1）")  # 版本
    enabled: bool = Field(default=False, description="默认关（gate by default）；启用走 settings/环境")  # 门控
    description: str = Field(default="", description="能力说明（路由/澄清展示）")  # 描述
    tools: list[SkillToolRole] = Field(default_factory=list, description="工具角色清单（引用 catalog）")  # 工具
    side_effect: _SIDE_EFFECT = Field(default="none", description="副作用等级")  # 副作用
    max_tools: int = Field(default=10, description="最多可用工具数（防超限）")  # 工具上限
    prompt: str = Field(default="", description="系统提示文本（或指向 PROMPT.md 的路径）")  # 提示
    requires: list[str] = Field(default_factory=list, description="运行前置（如 kb_index；缺则禁用并提示）")  # 前置
    trigger: list[str] = Field(default_factory=list, description="触发/边界关键词（路由参考，非规则）")  # 触发
    avoid: list[str] = Field(default_factory=list, description="反例/不该做的事（来自 verify/纠正）")  # 反例
    example_queries: list[str] = Field(default_factory=list, description="示例问题（评测/路由）")  # 示例
    cost_budget: CostBudget = Field(default_factory=CostBudget, description="成本预算")  # 成本
    meta: dict = Field(default_factory=dict, description="附加元数据（changelog/作者/来源 run_id）")  # 附加
