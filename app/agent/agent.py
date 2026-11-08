"""父图：只连边，不写业务（意图路由版）。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意类型

from langgraph.graph import END, START, StateGraph  # 图原语

from app.agent.nodes.clarify.interrupt import clarify_interrupt_node  # 澄清（可暂停）
from app.agent.nodes.context.node import load_context  # 上下文
from app.agent.nodes.dispatch.node import dispatch_node  # 固定路径直调
from app.agent.nodes.fallback.node import llm_fallback_node  # 兜底子图
from app.agent.nodes.finalize.node import finalize_node  # 收尾
from app.agent.nodes.general.node import general_agent_node  # L4e 通用兜底
from app.agent.nodes.preprocess.node import preprocess_node  # L1 预处理
from app.agent.nodes.pretriage.node import pretriage_node  # 前置短路分诊
from app.agent.nodes.route.node import rule_route_node  # 规则层
from app.agent.nodes.route_llm.node import route_llm_node  # LLM 兜底
from app.agent.nodes.semantic.node import semantic_node  # 语义层
from app.agent.nodes.triage.node import triage_node  # L3 分诊
from app.agent.nodes.verify.node import verify_node  # L5 校验
from app.agent.routing.edges import after_clarify, after_dispatch, after_pretriage, after_rule, after_semantic, after_triage  # 条件边
from app.agent.state import ChatState  # 状态


def build_route_graph(*, checkpointer: Any = None) -> Any:  # 意图路由父图
    """START → load_context → rule_route →(after_rule)→ semantic_router →(after_semantic)
    → dispatch_fixed | llm_fallback | clarify_or_end → finalize → END。"""
    graph = StateGraph(ChatState)  # 状态图
    graph.add_node("load_context", load_context)  # 上下文
    graph.add_node("preprocess", preprocess_node)  # L1 预处理
    graph.add_node("pretriage", pretriage_node)  # 前置短路分诊（纯规则）
    graph.add_node("rule_route", rule_route_node)  # 规则层
    graph.add_node("semantic_router", semantic_node)  # 语义层
    graph.add_node("route_llm", route_llm_node)  # LLM 兜底分类
    graph.add_node("triage", triage_node)  # L3 分诊
    graph.add_node("dispatch_fixed", dispatch_node)  # 固定路径
    graph.add_node("llm_fallback", llm_fallback_node)  # 兜底子图
    graph.add_node("general_agent", general_agent_node)  # L4e 通用兜底
    graph.add_node("verify", verify_node)  # L5 校验
    graph.add_node("clarify_or_end", clarify_interrupt_node)  # 澄清/拒绝（可暂停）
    graph.add_node("finalize", finalize_node)  # 收尾

    graph.add_edge(START, "load_context")  # 入口
    graph.add_edge("load_context", "preprocess")  # 上下文到预处理
    graph.add_edge("preprocess", "pretriage")  # 预处理到前置分诊
    graph.add_conditional_edges(  # 前置分诊：短路命中跳路由，未命中进入规则层
        "pretriage",  # 来源
        after_pretriage,  # 分支函数
        {  # 映射
            "dispatch_fixed": "dispatch_fixed",  # 命中快捷/补参/选择
            "llm_fallback": "llm_fallback",  # 命中强 ReAct
            "general_agent": "general_agent",  # 禁工具
            "clarify_or_end": "clarify_or_end",  # 澄清/拒绝
            "finalize": "finalize",  # 空/超长/注入拦截
            "rule_route": "rule_route",  # 未命中 → 走路由
            "fallback": END,  # 未知键兜底
        },  # 映射结束
    )  # 条件边结束
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
    graph.add_edge("route_llm", "triage")  # LLM 兜底后进入分诊
    graph.add_conditional_edges(  # 分诊后分支
        "triage",  # 来源
        after_triage,  # 分支函数
        {  # 映射
            "dispatch_fixed": "dispatch_fixed",  # fast_single
            "llm_fallback": "llm_fallback",  # fast_multi / agentic
            "general_agent": "general_agent",  # 通用兜底
            "clarify_or_end": "clarify_or_end",  # clarify / unsupported / 动作类
            "fallback": END,  # 未知键兜底
        },  # 映射结束
    )  # 条件边结束
    graph.add_edge("general_agent", "verify")  # 兜底后校验
    graph.add_conditional_edges(  # 固定路径后分支（质量不足→升级）
        "dispatch_fixed",  # 来源
        after_dispatch,  # 分支函数
        {  # 映射
            "llm_fallback": "llm_fallback",  # 升级 agentic
            "verify": "verify",  # 去校验
            "fallback": END,  # 未知键兜底
        },  # 映射结束
    )  # 条件边结束
    graph.add_edge("llm_fallback", "verify")  # 兜底后校验
    graph.add_edge("verify", "finalize")  # 校验后收尾
    graph.add_conditional_edges(  # 澄清/暂停后的分支
        "clarify_or_end",  # 来源
        after_clarify,  # 分支函数
        {  # 映射
            "dispatch_fixed": "dispatch_fixed",  # resume 选了固定路径
            "preprocess": "preprocess",  # 自由输入 → 重新路由
            "finalize": "finalize",  # 模板澄清 → 收尾
            "fallback": END,  # 未知键兜底
        },  # 映射结束
    )  # 条件边结束
    graph.add_edge("finalize", END)  # 收尾到结束
    return graph.compile(checkpointer=checkpointer)  # 编译