"""观测：检索结果 basis 携带 profile/paths；RagResult 默认档位。"""

from __future__ import annotations  # 延迟值注解

from types import SimpleNamespace  # 假结果

from app.agent.nodes.rag.node import RagResult  # 结果
from app.agent.tools.library.rag.search_knowledge_base import _data  # 工具出参


def test_rag_result_default_profile_paths() -> None:  # 默认档位/路径
    result = RagResult(evidence=[], hits=[], errors=[])  # 构造
    assert result.profile == "tool"  # 默认工具档
    assert result.paths == ["baseline"]  # 默认基线


def test_data_includes_profile_paths() -> None:  # 工具出参带 profile/paths
    result = SimpleNamespace(  # 假结果
        evidence=[],  # 证据
        gate={},  # 门限
        dense_count=1,  # 语义
        sparse_count=2,  # 关键词
        fused_count=2,  # 融合
        selected_count=0,  # 证据数
        profile="full",  # 档位
        paths=["baseline", "multi_query", "rerank"],  # 路径
    )  # 结束
    data = _data(result, query="q", mode="deep")  # 出参
    assert data["basis"]["profile"] == "full"  # 档位可见
    assert "multi_query" in data["basis"]["paths"]  # 路径可见
    assert data["query"] == "q" and data["mode"] == "deep"  # 归因字段