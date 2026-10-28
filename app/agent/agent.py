"""父图：只连边，不写业务。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意类型

from langgraph.graph import END, START, StateGraph  # 图原语

from app.agent.contracts import DecisionTarget  # 决策目标
from app.agent.nodes.answer.node import answer_node  # 生成
from app.agent.nodes.context.node import load_context  # 上下文
from app.agent.nodes.finalize.node import finalize_node  # 收尾
from app.agent.nodes.rag.entry import make_rag_node  # RAG 入口
from app.agent.nodes.rag.node import RagRunner  # 子图运行器
from app.agent.state import ChatState  # 状态


def after_rag(state: dict[str, Any]) -> str:  # RAG 后的分支
    """只读唯一决策对象 ``route``（旧状态退回 ``answerable``）。

    不能再用 ``state["evidence"]`` 判断：门限可能「reject 但 kept 非空」，检索节点已经
    迁移到 NO_EVIDENCE（FINALIZING），若这里仍按 evidence 路由到 answer，answer 再发
    GENERATION_DONE 就会 ``InvalidTransition: finalizing 不允许事件 generation_done``。
    """
    route = state.get("route")  # 唯一决策对象（模型或 dict）
    if route is not None:  # 新路径：读 RouteDecision.next
        target = route.get("next") if isinstance(route, dict) else getattr(route, "next", None)  # 兼容反序列化
        return "answer" if str(target) == DecisionTarget.ANSWER.value else "finalize"  # 按决策路由
    return "answer" if state.get("answerable") else "finalize"  # 旧状态兜底


def build_agent_graph(runner: RagRunner, *, checkpointer: Any = None):  # 建父图
    """START → load_context → rag → answer/finalize → END。"""
    graph = StateGraph(ChatState)  # 状态图
    graph.add_node("load_context", load_context)  # 上下文
    graph.add_node("rag", make_rag_node(runner))  # 检索子图
    graph.add_node("answer", answer_node)  # 生成
    graph.add_node("finalize", finalize_node)  # 收尾

    graph.add_edge(START, "load_context")  # 入口
    graph.add_edge("load_context", "rag")  # 上下文到检索
    graph.add_conditional_edges(  # 检索后分支
        "rag",  # 来源
        after_rag,  # 分支函数
        {"answer": "answer", "finalize": "finalize"},  # 映射
    )  # 条件边结束
    graph.add_edge("answer", "finalize")  # 生成后收尾
    graph.add_edge("finalize", END)  # 收尾到结束
    return graph.compile(checkpointer=checkpointer)  # 编译（E2 才挂 checkpointer）
