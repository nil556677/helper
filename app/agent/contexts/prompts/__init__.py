"""模板注册表：以「定义模块路径」为键；重复在构建时报错，不靠手写唯一名。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.contexts.prompts._types import PromptTemplate  # 模板类型
from app.agent.contexts.prompts.answer_rules import TEMPLATE as ANSWER_RULES  # S2 答题
from app.agent.contexts.prompts.repair_system import TEMPLATE as REPAIR_SYSTEM  # 修复
from app.agent.contexts.prompts.rewrite_system import TEMPLATE as REWRITE_SYSTEM  # 改写
from app.agent.contexts.prompts.summary_system import TEMPLATE as SUMMARY_SYSTEM  # 压缩
from app.agent.contexts.prompts.system_base import TEMPLATE as SYSTEM_BASE  # S1 全局

#: 供 spec 引用（避免手敲长路径）。
SYSTEM_BASE_UID = SYSTEM_BASE.uid  # S1
ANSWER_RULES_UID = ANSWER_RULES.uid  # S2
REPAIR_SYSTEM_UID = REPAIR_SYSTEM.uid  # 修复
REWRITE_SYSTEM_UID = REWRITE_SYSTEM.uid  # 改写
SUMMARY_SYSTEM_UID = SUMMARY_SYSTEM.uid  # 压缩


def build_registry(*templates: PromptTemplate) -> dict[str, PromptTemplate]:  # 建注册表
    """同一个 uid（定义模块）出现两次直接抛 ValueError。"""
    registry: dict[str, PromptTemplate] = {}  # 结果
    for template in templates:  # 逐个
        if template.uid in registry:  # 撞了
            raise ValueError(f"提示词模板重复定义：{template.uid!r}")  # 报错
        registry[template.uid] = template  # 登记
    return registry  # 返回


_REGISTRY = build_registry(  # 内置模板
    SYSTEM_BASE,  # S1
    ANSWER_RULES,  # S2
    REPAIR_SYSTEM,  # 修复
    REWRITE_SYSTEM,  # 改写
    SUMMARY_SYSTEM,  # 压缩
)  # 注册结束


def get(uid: str) -> PromptTemplate:  # 取模板
    """只接受 uid（定义模块路径）；不存在抛 KeyError。"""
    return _REGISTRY[uid]  # 查表


def names() -> tuple[str, ...]:  # 全部 uid
    """给测试/调试用。"""
    return tuple(_REGISTRY)  # 键集合


__all__ = [  # 对外导出
    "ANSWER_RULES_UID",  # S2
    "REPAIR_SYSTEM_UID",  # 修复
    "REWRITE_SYSTEM_UID",  # 改写
    "SUMMARY_SYSTEM_UID",  # 压缩
    "SYSTEM_BASE_UID",  # S1
    "build_registry",  # 建表
    "get",  # 取
    "names",  # 列
]
