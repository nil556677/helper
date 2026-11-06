"""L4b 多工具：检索变体在同一工具目录，且知识专家工具可解析。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.agents.experts import EXPERTS  # 专家注册
from app.agent.tools.catalog import tool_catalog  # 工具目录


def test_search_variants_in_catalog() -> None:  # 变体已注册
    catalog = tool_catalog()  # 目录
    assert "search_hybrid" in catalog  # 混合
    assert "search_deep" in catalog  # 深度
    assert "search_knowledge_base" in catalog  # 基础


def test_knowledge_agent_tools_resolvable() -> None:  # 知识专家工具都能解析
    descriptor, _build = EXPERTS["knowledge_agent"]  # 取描述符
    catalog = tool_catalog()  # 目录
    assert descriptor.tools  # 有工具
    assert all(name in catalog for name in descriptor.tools)  # 全部可解析（单源）
    assert "query_weather" in descriptor.tools  # agentic 也能查天气（消除降级）
    assert "current_time" in descriptor.tools  # agentic 也能查时间