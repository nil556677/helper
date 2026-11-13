"""上下文特性开关：生产开启、单测不破。

原理：开关挂在 `RunContext.features`（每 run 决定）。`stream` 创建 run 时置真 → 生产注入生效；
单元测试直接调 run_react/dispatch（无 RunContext）→ 读不到 → 保持既有手拼路径，测试不变。
"""

from __future__ import annotations  # 延迟值注解

import os  # 环境变量（灰度开关）

from app.agent.context import current_run  # 当前 run

__all__ = ["builder_enabled", "dispatch_context_enabled", "plan_execute_enabled"]  # 导出


def _feature(key: str) -> bool:  # 读单特性位
    """有 RunContext 且对应特性位置真才开；无 run（单测/独立调用）→ False。"""
    run = current_run()  # 当前 run（可能 None）
    if run is None:  # 无 run（单测/独立调用）
        return False  # 关
    features = getattr(run, "features", None) or {}  # 特性位
    return bool(features.get(key))  # 读


def builder_enabled() -> bool:  # 是否启用 ContextBuilder 注入（ReAct/general）
    """生产因为 stream 置 context_builder=True；unit 无 run=False。"""
    return _feature("context_builder")  # 特性位


def dispatch_context_enabled() -> bool:  # 是否启用 dispatch 上文拼接
    """独立于 ReAct：避免 ReAct 一开、dispatch 被连带开启上文的副作用。"""
    return _feature("dispatch_context")  # 特性位


def plan_execute_enabled() -> bool:  # 是否启用复杂分流走 Plan+Execute（灰度开关，环境变量）
    """`HELPER_PLAN_EXECUTE` 缺省 on；置 off 全部回现状单 ReAct，天然可回退。
    用环境变量而非 RunContext：路由层在节点装配前就要读，靠 run 特性位来不及。"""
    value = os.environ.get("HELPER_PLAN_EXECUTE", "on").strip().lower()  # 取值
    return value in ("1", "true", "yes", "on")  # 判断
