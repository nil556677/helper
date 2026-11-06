"""审批判定：按工具 `metadata.dsh.mutation` 判断目标专家是否需要 HITL。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.contracts import Mutation  # 变更画像
from app.agent.tools.catalog import tool_catalog  # 工具目录

__all__ = ["requires_approval"]  # 导出


def requires_approval(tool_names: tuple[str, ...], catalog: dict[str, Any] | None = None) -> bool:  # 是否需要审批
    """任一工具不是 READ_ONLY → 需要审批（风险由工具描述，不由专家属性决定）。"""
    tools = catalog if catalog is not None else tool_catalog()  # 目录（可注入便于测试）
    for name in tool_names or ():  # 逐工具
        tool = tools.get(name)  # 取工具
        meta = (getattr(tool, "metadata", None) or {}).get("dsh") if tool is not None else None  # 项目元数据
        if not isinstance(meta, dict):  # 没有元数据
            continue  # 跳过
        if meta.get("mutation") != Mutation.READ_ONLY:  # 非只读
            return True  # 需审批
    return False  # 全只读