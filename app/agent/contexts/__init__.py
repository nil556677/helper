"""类 C：上下文与提示词层（对外只从这里 import）。

- `assembler`  —— 仅承载 `ContextUsage` 占用统计容器（T0.4 起不再做装配）
- `blocks`     —— 标签块、转义、空块不输出
- `builder`    —— 唯一装配点（build_context：Prune→Restructure→Budget→Compose）
- `tokens`     —— 全仓唯一 token 估算
- `prompts`    —— 版本化模板 + 注册表
- `budget`     —— 窗口/阈值/触发判定（纯函数）
- `history`    —— 摘要 + 最近消息的投影
- `compact`    —— 压缩（滚动摘要引擎）
"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.contexts.assembler import ContextUsage  # 占用统计
from app.agent.contexts.budget import WINDOW, Budget  # 预算

__all__ = [  # 对外导出
    "WINDOW",  # 窗口常量
    "Budget",  # 预算
    "ContextUsage",  # 占用统计
]
