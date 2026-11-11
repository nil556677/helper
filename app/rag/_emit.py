"""把模型网关的 RETRY 等事件转发给当前 run（有 UI 消费者才发；无则静默）。

给 rewrite/hyde 这类 `ModelGateway(slot="llm")` 的网关当 `emit=` 用：让它们的重试事件
也能推给前端时间线，而不是被默认 `offline()` 吞掉。延迟导入 `current_run` 避免 import 环。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值


def forward(event: Any) -> None:  # 转发一条事件
    """有当前 run 才转发；没有（单测/独立调用）就静默丢弃。"""
    try:  # 取 run（惰性，防循环导入）
        from app.agent.context import current_run  # 当前 run

        run = current_run()  # 事件出口
    except Exception:  # 导入/取值失败
        run = None  # 视为无
    if run is None:  # 无消费者
        return  # 丢弃
    run.emit(event)  # 转发给前端