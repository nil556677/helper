"""工具目录：工具名 → 工具对象（供专家描述符按名解析，单一来源）。"""

from __future__ import annotations  # 延迟求值注解

from functools import lru_cache  # 进程级缓存
from typing import Any  # 任意值

__all__ = ["tool_catalog"]  # 导出


@lru_cache(maxsize=1)  # 只构建一次
def tool_catalog() -> dict[str, Any]:  # 名 → 工具
    """从内置工具库收集；与 `@tool_spec` 注册的那套完全一致。"""
    import app.agent.tools.library as _LIB  # 工具库（延迟导入避免环）
    from app.agent.tools.wiring import collect  # 收集

    return {tool.name: tool for tool in collect(_LIB)}  # 建映射