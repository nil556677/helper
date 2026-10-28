"""类 C：上下文与提示词层（对外只从这里 import）。

- `assembler`  —— 按固定顺序拼 system/messages 并统计占用
- `spec`       —— 每个 agent 的上下文声明（写死）
- `layers`     —— 九个数据块的纯渲染函数
- `blocks`     —— 标签块、转义、空块不输出
- `tokens`     —— 全仓唯一 token 估算
- `prompts`    —— 版本化模板 + 注册表
- `budget`     —— 窗口/阈值/触发判定（纯函数）
- `history`    —— 摘要 + 最近消息的投影
- `compact`    —— 压缩（C4）
"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.contexts.assembler import (  # 装配
    BuiltContext,  # 结果
    ContextAssembler,  # 门面
    ContextUsage,  # 占用统计
    build,  # 函数式入口
)
from app.agent.contexts.budget import WINDOW, Budget  # 预算
from app.agent.contexts.spec import AgentContextSpec  # 声明

__all__ = [  # 对外导出
    "WINDOW",  # 窗口常量
    "AgentContextSpec",  # agent 声明
    "Budget",  # 预算
    "BuiltContext",  # 装配结果
    "ContextAssembler",  # 装配门面
    "ContextUsage",  # 占用统计
    "build",  # 装配函数
]
