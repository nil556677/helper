"""自定义 ReAct 图：agent → approval_gate → tools → agent（含审批门/Limits）。

替换 `create_agent` 当选器——支持在 "模型决定用工具" 与 "真正执行工具" 之间插审批门。
审批 gate 只用 `should_require_approval`；无审批(tool approval=none/只读) → 直通 tools。
"""

from __future__ import annotations  # 延迟值注解

from typing import Annotated, Any, Literal, TypedDict  # 类型

from langchain_core.messages import SystemMessage, ToolMessage  # 消息
from langgraph.graph import END, START, StateGraph  # 图原语
from langgraph.graph.message import add_messages  # 消息累加
from langgraph.prebuilt import ToolNode  # 工具执行

from app.agent.react.policies.approval import should_require_approval  # 审批策略
from app.agent.tools.wiring import describe  # 工具描述符（读 approval）

__all__ = ["ReactState", "build_react"]  # 导出


class ReactState(TypedDict):  # ReAct 状态
    messages: Annotated[list, add_messages]  # 消息（累加）
    status: Literal["running", "done", "rejected", "expired"]  # 状态
    pending_request_ids: list[str]  # 待审请求
    approval_checked: bool  # 本轮是否已过审批门
    approval_blocked: bool  # 本轮是否因审批被拦截（回 agent 不再执行）


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

    # —— approval_gate：该不该审；审则拦截该调用 + 落表(best-effort) ——
    def approval_gate(state: ReactState) -> dict[str, Any]:  # 审批门
        if state.get("approval_checked"):  # 本轮已判过
            return {"approval_checked": False}  # 复位，下一轮再判
        call = (state.get("messages") or [])[-1]  # 末尾模型（带 tool_calls）
        need: list[Any] = []  # 需审批的调用
        for tc in getattr(call, "tool_calls", None) or []:  # 逐个调用
            tool = next((t for t in tools if t.name == tc.get("name")), None)  # 找工具
            if tool is None:  # 未知工具
                continue  # 跳过
            want, _reason = should_require_approval(describe(tool), args=tc.get("args") or {})  # 判定
            if want:  # 需审
                need.append(tc)  # 记
        if not need:  # 无审批需求 → 直通 tools
            return {"approval_checked": True}  # 放行
        # 需审批：落表(best-effort) + 用 ToolMessage 拦截本次执行，回模型如实说明
        import time as _time  # 时间

        try:  # 落表失败不阻断拦截
            from app.agent.approval import repository as approval_repo  # 审批仓储

            for tc in need:  # 逐调用
                approval_repo.create_request(  # 落一条 pending
                    thread_id=str(state.get("thread_id") or ""),  # 线程
                    tenant_id=str(state.get("tenant_id") or "default"),  # 租户
                    requester_id=str(state.get("requester_id") or "user"),  # 发起
                    tool_name=str(tc.get("name") or ""),  # 工具
                    args_redacted=dict(tc.get("args") or {}),  # 参数
                    resource_summary=f"需要审批：{tc.get('name')}",  # 摘要
                    mutation="read_only",  # 变更画像
                    risk="medium",  # 风险
                    approval_reason="工具策略要求人工审批",  # 原因
                    expires_at=_time.time() + 3600,  # 1 小时
                )  # 结束
        except Exception:  # 未配置库等
            pass  # 仍拦截
        blocked: list[Any] = []  # 拦截消息
        for tc in need:  # 逐调用
            blocked.append(  # 生成 ToolMessage（不执行该工具）
                ToolMessage(
                    content=f"工具 {tc.get('name')} 需要人工审批，已登记待审，本次不执行。",
                    tool_call_id=str(tc.get("id") or ""),
                    name=str(tc.get("name") or ""),
                )
            )  # 追加
        return {"messages": blocked, "approval_checked": True, "approval_blocked": True, "status": "running"}  # 回模型

    # —— 路由边 ——
    def should_continue(state: ReactState) -> str:  # agent → approval or end
        return "approval_gate" if _has_tool_calls(state) else END  # 有调用进审批门，无则收尾

    def after_approval(state: ReactState) -> str:  # 审批门 → tools / agent / end
        if state.get("status") in ("rejected", "expired"):  # 拒绝/过期
            return END  # 停
        if state.get("approval_blocked"):  # 已拦截（回模型说明）
            return "agent"  # 回模型
        return "tools"  # 审批通过执行

    g = StateGraph(ReactState)  # 图
    g.add_node("agent", agent_node)  # 模型
    g.add_node("approval_gate", approval_gate)  # 审批门
    g.add_node("tools", tool_node)  # 工具
    g.add_edge(START, "agent")  # 入口
    g.add_conditional_edges("agent", should_continue, {"approval_gate": "approval_gate", END: END})  # agent 分流
    g.add_conditional_edges("approval_gate", after_approval, {"tools": "tools", "agent": "agent", END: END})  # 门分流
    g.add_edge("tools", "agent")  # 工具回模型
    return g.compile()  # 编译