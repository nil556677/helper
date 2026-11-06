"""路由人话标签：澄清卡等界面展示用（id 不直接给人看）。"""

from __future__ import annotations  # 延迟求值注解

from functools import lru_cache  # 进程级缓存

from app.agent.routing.rules.loader import load_routes  # 路由定义

__all__ = ["route_label", "route_labels"]  # 导出


@lru_cache(maxsize=1)  # 只建一次
def route_labels() -> dict[str, str]:  # id → 人话
    """id 为键；label 为空则回退 id。"""
    return {spec.route_id: (spec.label or spec.route_id) for spec in load_routes()}  # 建表


def route_label(route_id: str) -> str:  # 单个标签
    return route_labels().get(str(route_id), str(route_id))  # 找不到回退 id