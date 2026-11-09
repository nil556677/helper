"""L1 预处理层：实体补全 / 时间解析 / flags（**零 LLM**；查询改写归 RAG full 档，不在此层）。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.preprocess.entities import extract_entities, inherit_from_turns  # 实体
from app.agent.preprocess.flags import FLAG_KEYS, collect_flags  # flags
from app.agent.preprocess.timeparse import resolve_time  # 时间

__all__ = [  # 导出
    "FLAG_KEYS",  # 允许的 flag
    "collect_flags",  # flags 收集
    "extract_entities",  # 显式实体
    "inherit_from_turns",  # 历史继承
    "resolve_time",  # 时间解析
]