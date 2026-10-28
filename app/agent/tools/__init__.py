"""工具层：`@tool` 收集器与 `ToolManager`。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.tools.manager import (  # 管理器
    DuplicateToolError,  # 重名
    InvalidToolNameError,  # 非法名
    ToolManager,  # 管理器
    collect_tools,  # 收集器
)  # 导入结束

__all__ = ["DuplicateToolError", "InvalidToolNameError", "ToolManager", "collect_tools"]  # 导出
