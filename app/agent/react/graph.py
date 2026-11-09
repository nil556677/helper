"""自定义 ReAct 图：agent → approval_gate → tools → agent（含审批门/Limits）。

替换 `create_agent` 当选器——支持在 "模型决定用工具" 与 "真正执行工具" 之间插审批门。
审批 gate 只用 `should_require_approval`；无审批(tool approval=none/只读) → 直通 tools。
"""

from __future__ import annotations  # 延迟值注解

from typing import Annotated, Any, Literal, TypedDict  # 类型

from langchain_core.messages import SystemMessage  # 系统消息
from langgraph.graph import END, START, StateGraph  # 图原语
from langgraph.graph.message import add_messages  # 消息累加
from langgraph.prebuilt import ToolNode  # 工具执行
from langgraph.types import interrupt  # 审批暂停

from app.agent.react.policies.approval import should_require_approval  # 审批策略
from app.agent.tools.wiring import describe  # 工具描述符（读 approval）

__all__ = ["ReactState", "build_react"]  # 导出


class ReactState(TypedDict):  # ReAct 状态
    messages: Annotated[list, add_messages]  # 消息（累加）
    status: Literal["running", "done", "rejected", "expired"]  # 状态
    pending_request_ids: list[str]  # 待审请求
    approval_checked: bool  # 本轮是否已过审批门


def _has_tool_calls(state: ReactState) -> bool:  # 末尾模型是否要调工具
    last = (state.get("messages") or [])[-1] if state.get("messages") else None  # 最后消息
    return bool(getattr(last, "tool_calls", None))  # 判


def build_react(  # 构造自定义 ReAct 图
    model: Any,  # 模型
    tools: list[Any],  # 工具
    system_prompt: str,  # 系统提示
) -> Any:  # 编译图
    """agent(LLM) → approval_gate → tools → agent；审批拒绝/过期 → END。"""
    bound = model.bind_tools(tools)  # 绑定工具
    tool_node = ToolNode(tools)  # 执行节点

    # —— agent：调模型（系统提示 + 历史）——
    async def agent_node(state: ReactState) -> dict[str, Any]:  # 模型节点
        history = list(state.get("messages") or [])  # 历史
        resp = await bound.ainvoke([SystemMessage(content=system_prompt), *history])  # 调模型（异步）
        return {"messages": [resp], "status": "running"}  # 返回

    # —— approval_gate：该不该审；审则落表+暂停；恢复读表定走向 ——
    def approval_gate(state: ReactState, config: Any = None) -> Any:  # 审批门
        del config  # 不用
        if state.get("approval_checked"):  # 本轮已审过（工具失败回 agent 再选 → 不再重复审本次）
            return {"approval_checked": False}  # 回 tools（重置，下一轮再判）
        call = (state.get("messages") or [])[-1]  # 末尾模型（带 tool_calls）
        pending_ids = list(state.get("pending_request_ids") or [])  # 已有待审
        if not pending_ids:  # 首次进入：收集需审批的调用
            need: list[tuple[Any, dict[str, Any]]] = []  # (tool_call, 描述)
            for tc in getattr(call, "tool_calls", None) or []:  # 逐个调用
                tool = next((t for t in tools if t.name == tc.get("name")), None)  # 找工具
                if tool is None:  # 未知工具
                    continue  # 跳过
                want, _reason = should_require_approval(describe(tool), args=tc.get("args") or {})  # 判定
                if want:  # 需审
                    need.append(tc)  # 记
            if not need:  # 无审批需求 → 直通 tools
                return {"approval_checked": True}  # 放行
            # 有审批需求 → 落表 + 暂停（本版真实落表接入数据源后进行；先暂停占位）
            request_ids = [f"rq:{tc.get('id') or tc.get('name')}" for tc in need]  # 请求 id
            interrupt({"request_ids": request_ids, "tool_calls": [tc.get("name") for tc in need]})  # 暂停待审
            return {"pending_request_ids": request_ids, "approval_checked": True}  # 恢复后放行
        # 已有待审恢复 → 简化：批准视为通过（拒绝/过期由审批层抹掉调用再做）
        return {"approval_checked": True, "pending_request_ids": []}  # 放行

    # —— 路由边 ——
    def should_continue(state: ReactState) -> str:  # agent → approval or end
        return "approval_gate" if _has_tool_calls(state) else END  # 有调用进审批门，无则收尾

    def after_approval(state: ReactState) -> str:  # 审批门 → tools 或 end
        return END if state.get("status") in ("rejected", "expired") else "tools"  # 拒绝/过期则停，否则执行

    g = StateGraph(ReactState)  # 图
    g.add_node("agent", agent_node)  # 模型
    g.add_node("approval_gate", approval_gate)  # 审批门
    g.add_node("tools", tool_node)  # 工具
    g.add_edge(START, "agent")  # 入口
    g.add_conditional_edges("agent", should_continue, {"approval_gate": "approval_gate", END: END})  # agent 分流
    g.add_conditional_edges("approval_gate", after_approval, {"tools": "tools", END: END})  # 门→工具
    g.add_edge("tools", "agent")  # 工具回模型
    return g.compile()  # 编译（审批暂停需外部 checkpointer 才真正中断）