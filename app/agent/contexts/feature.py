"""上下文特性开关：生产开启、单测不破。

原理：开关挂在 `RunContext.features`（每 run 决定）。`stream` 创建 run 时置真 → 生产注入生效；
单元测试直接调 run_react/dispatch（无 RunContext）→ 读不到 → 保持既有手拼路径，测试不变。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from app.agent.context import current_run  # 当前 run

__all__ = ["builder_enabled"]  # 导出


def builder_enabled() -> bool:  # 是否启用 ContextBuilder 注入
    """只在有 RunContext 且特性位置真时开（生产接线；无 run=测试走旧路径）。"""
    run = current_run()  # 当前 run（可能 None）
    if run is None:  # 无 run（单测/独立调用）
        return False  # 走旧路径
    features = getattr(run, "features", None) or {}  # 特性位
    return bool(features.get("context_builder"))  # 开关