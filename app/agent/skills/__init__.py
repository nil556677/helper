"""Skill 系统：能力包（manifest + 工具角色 + 门控 + 版本），调用仍走工具。

- 发现/校验/门控/组装见 `registry`；审计见 `store`；
- 与 MCP 两向：消费 `mcp`（EXTRA_TOOLS）/ 暴露 `mcp_server`；
- 沉淀流水线：`condenser`（提炼草案）+ `candidates`（候选区/校验 Gate/入库）。
"""

from app.agent.skills.models import SkillManifest, SkillToolRole  # 模型
from app.agent.skills.registry import (  # 注册
    EXTRA_TOOLS,
    assemble,
    discover,
    enabled,
    registered,
    resolve_tools,
    validate,
)

__all__ = ["EXTRA_TOOLS", "SkillManifest", "SkillToolRole", "assemble", "discover", "enabled", "registered", "resolve_tools", "validate"]  # 导出
