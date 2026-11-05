"""节点完成 → 状态迁移事件 的投影（Q3/P3：driver 据此驱动状态机）。

**职责**：父图节点只返回 state update，不再自行调用 ``ctx.transition``；状态机迁移权
统一收归 driver。driver 用 ``astream(stream_mode=["updates","values"])`` 观测每个节点的
完成，把「已完成节点名 + 该节点更新」投影成对应的 ``RunEvent``，由 driver 统一执行迁移。

为什么需要投影表：图与状态机是两套表达（节点名 vs RunEvent），没有这张表 driver 就不知道
哪个节点对应哪次迁移。投影是**确定性**的：父图结构固定（load_context → rag → answer/finalize），
因此节点完成顺序固定，迁移必然合法（不会 InvalidTransition）。
"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from app.agent.contracts import DecisionTarget  # 决策目标枚举（route.next 判断）
from app.agent.fsm.states import RunEvent  # 迁移事件

#: 父图顶层节点名 → 完成时触发的事件；rag 需要读 route 才能定（agent/finalize）。
SIMPLE_NODE_EVENT: dict[str, RunEvent] = {  # 固定映射
    "load_context": RunEvent.CONTEXT_LOADED,  # 上下文就绪（loading → retrieving）
    "answer": RunEvent.GENERATION_DONE,  # 生成完成（generating → finalizing）
    "agent": RunEvent.GENERATION_DONE,  # 工具循环完成（generating → finalizing）
    # —— 意图路由父图（build_route_graph）——
    "rule_route": RunEvent.RETRIEVAL_DONE,  # 路由/检索阶段完成（retrieving → generating）
    "dispatch_fixed": RunEvent.GENERATION_DONE,  # 固定路径回答完成
    "llm_fallback": RunEvent.GENERATION_DONE,  # 兜底子图回答完成
    "clarify_or_end": RunEvent.GENERATION_DONE,  # 澄清/拒绝（即最终回应）
    "finalize": RunEvent.FINALIZED,  # 收尾完成（finalizing → done）
}  # 固定映射结束


def project_event(node: str, update: dict[str, Any] | None) -> RunEvent | None:  # 投影
    """把「已完成节点 + 其更新」投影成 RunEvent；不认识/不需要迁移返回 None。

    - ``rag`` 依据 ``update["route"]["next"]``：ANSWER → RETRIEVAL_DONE，否则 NO_EVIDENCE；
    - 其余节点按 :data:`SIMPLE_NODE_EVENT` 固定映射；
    - 子图内部节点（rewrite 等）不会出现在父图顶层更新里，天然被忽略。
    """
    if node == "rag":  # 检索完成：走 answer 还是 finalize 由唯一决策对象 route 决定
        route: dict[str, Any] = dict((update or {}).get("route") or {})  # 读决策对象
        if str(route.get("next")) == DecisionTarget.ANSWER.value:  # 能生成
            return RunEvent.RETRIEVAL_DONE  # retrieving → generating
        return RunEvent.NO_EVIDENCE  # 无证据：retrieving → finalizing
    return SIMPLE_NODE_EVENT.get(node)  # 其余固定映射（不存在返回 None）
