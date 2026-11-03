"""父图：只连边，不写业务。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意类型

from langgraph.graph import END, START, StateGraph  # 图原语

from app.agent.contracts import DecisionTarget  # 决策目标
from app.agent.nodes.agent.node import tool_agent_node  # 工具循环
from app.agent.nodes.context.node import load_context  # 上下文
from app.agent.nodes.finalize.node import finalize_node  # 收尾
from app.agent.nodes.rag.entry import make_rag_node  # RAG 入口
from app.agent.nodes.rag.node import RagRunner  # 子图运行器
from app.agent.state import ChatState  # 状态


def after_rag(state: dict[str, Any]) -> str:  # RAG 后的分支
    """只读唯一决策对象 ``route``（不再看 evidence/answerable）。"""
    return (  # 路由
        "agent"  # 有证据 -> 工具循环
        if state["route"]["next"] == DecisionTarget.ANSWER.value  # 能生成
        else "finalize"  # 无证据 -> 直接收尾
    )  # 返回结束


def build_agent_graph(runner: RagRunner, *, checkpointer: Any = None):  # 建父图
    """START → load_context → rag → agent(工具循环)/finalize → END。"""
    graph = StateGraph(ChatState)  # 状态图
    graph.add_node("load_context", load_context)  # 上下文
    graph.add_node("rag", make_rag_node(runner))  # 检索子图
    graph.add_node("agent", tool_agent_node)  # 工具循环
    graph.add_node("finalize", finalize_node)  # 收尾

    graph.add_edge(START, "load_context")  # 入口
    graph.add_edge("load_context", "rag")  # 上下文到检索
    graph.add_conditional_edges(  # 检索后分支
        "rag",  # 来源
        after_rag,  # 分支函数
        {"agent": "agent", "finalize": "finalize"},  # 映射
    )  # 条件边结束
    graph.add_edge("agent", "finalize")  # 工具循环后收尾
    graph.add_edge("finalize", END)  # 收尾到结束
    return graph.compile(checkpointer=checkpointer)  # 编译（E2 才挂 checkpointer）