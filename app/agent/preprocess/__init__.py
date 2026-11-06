"""L1 预处理层：实体补全 / 时间解析 / flags / Lazy Rewrite。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.preprocess.entities import extract_entities, inherit_from_turns  # 实体
from app.agent.preprocess.flags import FLAG_KEYS, collect_flags  # flags
from app.agent.preprocess.rewrite import RewriteFn, maybe_rewrite, needs_rewrite  # 改写
from app.agent.preprocess.timeparse import resolve_time  # 时间

__all__ = [  # 导出
    "FLAG_KEYS",  # 允许的 flag
    "RewriteFn",  # 改写签名
    "collect_flags",  # flags 收集
    "extract_entities",  # 显式实体
    "inherit_from_turns",  # 历史继承
    "maybe_rewrite",  # 可能改写
    "needs_rewrite",  # 是否需改写
    "resolve_time",  # 时间解析
]