"""L4b 检索工具：合并为单一工具（mode 区分强度），知识专家工具可解析。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 跑异步工具
from types import SimpleNamespace  # 假结果

from app.agent.agents.experts import EXPERTS  # 专家注册
from app.agent.tools.catalog import tool_catalog  # 工具目录
from app.agent.tools.library.rag.search_knowledge_base import KnowledgeQuery  # 参数契约


def test_single_search_tool_in_catalog() -> None:  # 只剩一个检索工具
    catalog = tool_catalog()  # 目录
    assert "search_knowledge_base" in catalog  # 基础检索
    assert "search_hybrid" not in catalog  # 已合并
    assert "search_deep" not in catalog  # 已合并


def test_knowledge_query_mode_enum() -> None:  # mode 是枚举，模型不易填错
    assert set(KnowledgeQuery.model_fields) == {"query", "mode", "top_k"}  # 三个字段
    assert KnowledgeQuery(query="x").mode == "fast"  # 默认快速
    assert KnowledgeQuery(query="x", mode="deep").mode == "deep"  # 可指定深度


def test_knowledge_agent_tools_resolvable() -> None:  # 知识专家工具都能解析
    descriptor, _build = EXPERTS["knowledge_agent"]  # 取描述符
    catalog = tool_catalog()  # 目录
    assert descriptor.tools  # 有工具
    assert all(name in catalog for name in descriptor.tools)  # 全部可解析（单源）
    assert "query_weather" in descriptor.tools  # agentic 也能查天气（消除降级）
    assert "current_time" in descriptor.tools  # agentic 也能查时间


def test_mode_maps_profile_and_top_k(monkeypatch) -> None:  # mode → profile/top_k 映射
    import importlib  # 取模块（包 __init__ 里同名是工具）

    mod = importlib.import_module("app.agent.tools.library.rag.search_knowledge_base")  # 工具模块

    seen: dict = {}  # 记录调用参数

    class _Runner:  # 假运行器
        async def run(self, query, *, profile="tool", top_k=6, rerank=True):  # 跑
            seen.update(profile=profile, top_k=top_k, rerank=rerank, query=query)  # 记录
            return SimpleNamespace(evidence=[{"text": "a", "source_name": "s"}], gate={}, dense_count=1, sparse_count=1, fused_count=1, selected_count=1, profile=profile, paths=["baseline"])  # 结果

    monkeypatch.setattr(mod, "_runner", lambda: _Runner())  # 打桩
    env = asyncio.run(mod.search_knowledge_base.ainvoke({"query": "q", "mode": "deep"}))  # 深度
    from app.agent.tools.wiring import unwrap_tool_output  # 还原

    out = unwrap_tool_output(env)  # 包络
    assert out.data["mode"] == "deep"  # data 带 mode
    assert out.data["query"] == "q"  # data 带 query（归因）
    assert seen["profile"] == "deep"  # 深度档
    assert seen["top_k"] == 20  # 按 mode 自动条数