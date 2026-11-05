"""装配器：生产入口已切到新路由图。"""

from __future__ import annotations  # 延迟求值注解

import app.agent.assembly as assembly  # 被测


def test_assembly_builds_route_graph() -> None:  # 生产装配用新图
    assembly.reset()  # 清缓存
    graph = assembly.build()  # 建图
    assert graph is not None  # 有图
    assert assembly.build() is graph  # 进程内复用
    # 新图必须包含路由节点（旧图没有 rule_route）
    nodes = set(graph.get_graph().nodes)  # 节点集合
    assert {"rule_route", "semantic_router", "dispatch_fixed", "llm_fallback", "clarify_or_end"} <= nodes  # 新节点齐
    assembly.reset()  # 收尾清理