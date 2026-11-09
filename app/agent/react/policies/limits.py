"""ReAct 限额常量（显式配置，供图/工具门复用）。"""

from __future__ import annotations  # 延迟值注解

__all__ = ["MAX_PER_TOOL", "RECURSION_LIMIT", "TOKEN_BUDGET_PER_REQUEST"]  # 导出

#: 单工具最大尝试次数（触顶换策略）。
MAX_PER_TOOL = 3  # 次
#: 图递归上限（防死循环）。
RECURSION_LIMIT = 10  # 步
#: 单请求 token 预算。
TOKEN_BUDGET_PER_REQUEST = 20000  # token