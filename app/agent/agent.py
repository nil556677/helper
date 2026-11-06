"""父图：只连边，不写业务（意图路由版）。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意类型

from langgraph.graph import END, START, StateGraph  # 图原语

from app.agent.nodes.clarify.node import clarify_node  # 澄清/拒绝
from app.agent.nodes.context.node import load_context  # 上下文
from app.agent.nodes.dispatch.node import dispatch_node  # 固定路径直调
from app.agent.nodes.fallback.node import llm_fallback_node  # 兜底子图
from app.agent.nodes.finalize.node import finalize_node  # 收尾
from app.agent.nodes.preprocess.node import preprocess_node  # L1 预处理
from app.agent.nodes.route.node import rule_route_node  # 规则层
from app.agent.nodes.route_llm.node import route_llm_node  # LLM 兜底
from app.agent.nodes.semantic.node import semantic_node  # 语义层
from app.agent.routing.edges import after_llm, after_rule, after_semantic  # 条件边
from app.agent.state import ChatState  # 状态


def build_route_graph(*, checkpointer: Any = None) -> Any:  # 意图路由父图
    """START → load_context → rule_route →(after_rule)→ semantic_router →(after_semantic)
    → dispatch_fixed | llm_fallback | clarify_or_end → finalize → END。"""
    graph = StateGraph(ChatState)  # 状态图
    graph.add_node("load_context", load_context)  # 上下文
    graph.add_node("preprocess", preprocess_node)  # L1 预处理
    graph.add_node("rule_route", rule_route_node)  # 规则层
    graph.add_node("semantic_router", semantic_node)  # 语义层
    graph.add_node("route_llm", route_llm_node)  # LLM 兜底分类
    graph.add_node("dispatch_fixed", dispatch_node)  # 固定路径
    graph.add_node("llm_fallback", llm_fallback_node)  # 兜底子图
    graph.add_node("clarify_or_end", clarify_node)  # 澄清/拒绝
    graph.add_node("finalize", finalize_node)  # 收尾

    graph.add_edge(START, "load_context")  # 入口
    graph.add_edge("load_context", "preprocess")  # 上下文到预处理
    graph.add_edge("preprocess", "rule_route")  # 预处理到规则
    graph.add_conditional_edges(  # 规则后分支
        "rule_route",  # 来源
        after_rule,  # 分支函数
        {"dispatch_fixed": "dispatch_fixed", "semantic_router": "semantic_router", "fallback": END},  # 映射（含 END 兜底）
    )  # 条件边结束
    graph.add_conditional_edges(  # 语义后分支（三态）
        "semantic_router",  # 来源
        after_semantic,  # 分支函数
        {  # 映射
            "dispatch_fixed": "dispatch_fixed",  # direct
            "route_llm": "route_llm",  # ambiguous → LLM 兜底
            "clarify_or_end": "clarify_or_end",  # miss
            "fallback": END,  # 未知键兜底
        },  # 映射结束
    )  # 条件边结束
    graph.add_conditional_edges(  # LLM 兜底后分支
        "route_llm",  # 来源
        after_llm,  # 分支函数
        {  # 映射
            "llm_fallback": "llm_fallback",  # query/action → 兜底子图
            "clarify_or_end": "clarify_or_end",  # clarify
            "fallback": END,  # 未知键兜底
        },  # 映射结束
    )  # 条件边结束
    graph.add_edge("dispatch_fixed", "finalize")  # 固定路径后收尾
    graph.add_edge("llm_fallback", "finalize")  # 兜底后收尾
    graph.add_edge("clarify_or_end", "finalize")  # 澄清后收尾
    graph.add_edge("finalize", END)  # 收尾到结束
    return graph.compile(checkpointer=checkpointer)  # 编译