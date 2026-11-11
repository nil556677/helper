"""自定义 ReAct 图：agent → approval_gate → tools → agent（含审批门/Limits）。

替换 `create_agent` 当选器——支持在 "模型决定用工具" 与 "真正执行工具" 之间插审批门。
审批 gate 只用 `should_require_approval`；无审批(tool approval=none/只读) → 直通 tools。
"""

from __future__ import annotations  # 延迟值注解

from collections.abc import AsyncIterator, Callable  # 类型
from typing import Annotated, Any, Literal, TypedDict  # 类型

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage  # 消息
from langgraph.graph import END, START, StateGraph  # 图原语
from langgraph.graph.message import add_messages  # 消息累加
from langgraph.prebuilt import ToolNode  # 工具执行

from langgraph.types import interrupt  # 审批暂停（恢复模式）

from app.agent.context import current_run  # 当前 run（发 RETRY 事件）
from app.agent.react.policies.approval import should_require_approval  # 审批策略
from app.agent.react.policies.limits import MAX_PER_TOOL  # 单工具失败熔断阈值
from app.agent.tools.wiring import describe  # 工具描述符（读 approval）
from app.core.retry import RetryPolicy, retry_async  # 重试（按错误类型、仅首 token 前）

__all__ = ["ReactState", "approval_stats", "build_react"]  # 导出

#: 进程级审批计数（观测用）。
_approval_stats: dict[str, int] = {"blocked": 0, "created": 0}  # 计数


def _circuit_blocked_names(state: ReactState) -> set[str]:  # 已触发熔断的工具名
    """统计消息历史里各工具失败的 ToolMessage 次数，≥ MAX_PER_TOOL 视为熔断。

    用 `ToolMessage.status != "success"` 判定失败（与兜底 agent._message 判定一致）。
    熔断后即拦截该工具，避免模型对它反复失败调用烧 token / 打后端。
    """
    counts: dict[str, int] = {}  # 工具 → 失败次数
    for msg in state.get("messages") or []:  # 逐条消息
        if not isinstance(msg, ToolMessage):  # 只看工具结果
            continue  # 跳过
        name = str(getattr(msg, "name", "") or "")  # 工具名
        if not name:  # 无名
            continue  # 跳过
        st = getattr(msg, "status", None)  # 状态
        failed = bool(st and str(st) != "success")  # 非 success 即失败
        if failed:  # 失败
            counts[name] = counts.get(name, 0) + 1  # 计数
    return {name for name, c in counts.items() if c >= MAX_PER_TOOL}  # 达标即熔断


def approval_stats() -> dict[str, int]:  # 审批统计
    """被审批门拦截次数 / 落表登记次数（累计，观测用）。"""
    return dict(_approval_stats)  # 拷贝


#: 保密提示（仅一行附注，不进审核正文/审计）。
CONFIDENTIAL_NOTICE = "（订单属保密数据，请勿截图或传播；本次已留痕审计）"  # 提示


def _approval_summary(name: str, args: dict[str, Any]) -> str:  # 待确认内容摘要
    """把将展示给用户的"被审核内容"翻成中文摘要，供用户判断批不批。"""
    if name == "query_order":  # 订单
        key = str(args.get("order_id") or args.get("phone") or "?")  # 单号/手机号
        return f"查看订单 {key} 的『订单状态 / 金额 / 支付方式 / 下单时间』"  # 摘要
    if name == "query_logistics":  # 物流
        key = str(args.get("order_id") or args.get("tracking_no") or "?")  # 单号/运单号
        return f"查看订单 {key} 的『物流轨迹 / 快递公司 / 配送状态』"  # 摘要
    return f"调用 {name}：{args}"  # 通用


class ReactState(TypedDict):  # ReAct 状态
    messages: Annotated[list, add_messages]  # 消息（累加）
    status: Literal["running", "done", "rejected", "expired"]  # 状态
    pending_request_ids: list[str]  # 待审请求
    approval_checked: bool  # 本轮是否已过审批门
    approval_blocked: bool  # 本轮是否因审批被拦截（回 agent 不再执行）
    approval_pending: list[Any]  # 待审批元信息（request_id/tool_name/summary）→ 上抛路由级弹框


def _has_tool_calls(state: ReactState) -> bool:  # 末尾模型是否要调工具
    last = (state.get("messages") or [])[-1] if state.get("messages") else None  # 最后消息
    return bool(getattr(last, "tool_calls", None))  # 判


def _chunk_text(chunk: Any) -> str:  # 流式块 → 文本
    """兼容 content 为 str 或多段列表（AIMessageChunk）。"""
    content = getattr(chunk, "content", "")  # 内容
    if isinstance(content, str):  # 纯字符串
        return content  # 返回
    if isinstance(content, list):  # 多段
        return "".join(  # 拼接
            str(part.get("text") or "")  # 只取文本块
            for part in content  # 逐段
            if isinstance(part, dict)  # 字典块
        )  # 返回
    return ""  # 认不出


def _emit_retry(info: Any) -> None:  # 发重试事件（有 run 才发，无则静默）
    """把 RetryInfo 转成 RETRY 事件给前端；只在有 UI 消费者时发。"""
    run = current_run()  # 事件出口
    if run is None:  # 无 run
        return  # 跳过
    try:  # 转发失败不影响主流程
        from app.agent.contracts import EventType, make  # 事件构造

        run.emit(  # 发
            make(  # 构造
                EventType.RETRY,  # 类型
                attempt=int(getattr(info, "attempt", 0)),  # 第几次
                max_attempts=int(getattr(info, "max_attempts", 0)),  # 上限
                error_kind=str(getattr(info, "error_kind", "")),  # 类别
                error_message=str(getattr(info, "error_message", "")),  # 信息
                delay_ms=int(getattr(info, "delay_ms", 0)),  # 等待
                scope=str(getattr(info, "scope", "model")),  # 作用域
            )  # 构造结束
        )  # 发结束
    except Exception:  # 事件构造失败
        return  # 忽略


async def _aclose_stream(source: Any) -> None:  # 关上游流
    """取消/失败时把 HTTP 连接关掉，别让它继续生成。"""
    closer = getattr(source, "aclose", None)  # 异步关闭器
    if closer is None:  # 没有
        return  # 跳过
    try:  # 关闭可能因耗尽报错
        await closer()  # 关
    except Exception:  # 关失败不影响
        return  # 忽略


async def _astream_retry(  # 带重试的流式（仅首 token 前重试 + 发 RETRY 事件）
    bound: Any,  # 已绑定工具/参数的模型
    messages: list[Any],  # 请求消息
) -> AsyncIterator[Any]:  # 逐块
    """与 ModelGateway.astream 同语义：首 token 之前的错误按类型重试（瞬时可重试/配置不重试），
    一旦出字就交给调用方处理中途失败；每次重试发 RETRY 事件到前端。
    """
    policy = RetryPolicy()  # 默认策略（最多 3 次、指数退避）

    async def open_stream() -> tuple[Any, Any]:  # 建流并取首块
        """首 token 的错误才算「还没开始」，可重试。"""
        source = bound.astream(messages)  # 上游流
        try:  # 取首块
            first = await source.__anext__()  # 首 token
        except StopAsyncIteration:  # 空流
            await _aclose_stream(source)  # 关
            return source, None  # 无首块
        except BaseException:  # 首 token 前失败
            await _aclose_stream(source)  # 关
            raise  # 交给重试
        return source, first  # 成功

    source, first = await retry_async(  # 首 token 前可重试
        open_stream, policy, on_retry=_emit_retry, scope="model"  # 事件 + 作用域
    )  # 重试结束
    try:  # 保证关流
        if first is not None:  # 有首块
            yield first  # 先给
        async for item in source:  # 后续块
            yield item  # 逐块
    finally:  # 无论怎样
        await _aclose_stream(source)  # 关上游


def build_react(  # 构造自定义 ReAct 图
    model: Any,  # 模型
    tools: list[Any],  # 工具
    system_prompt: str,  # 系统提示
    *,  # 关键字
    checkpointer: Any = None,  # 检查点（恢复模式必需）
    interrupt_on_approval: bool = False,  # 需审批是否真正暂停等待（默认拦截不执行）
    token_sink: Callable[[str], None] | None = None,  # 逐 token 回调（发 DELTA / 记 partial / 标首 token）
) -> Any:  # 编译图
    """agent(LLM) → approval_gate → tools → agent；审批拒绝/过期 → END。

    `interrupt_on_approval` 需配合 `checkpointer`：需审批 → interrupt 暂停，
    `Command(resume)` 恢复后读审批表：approved→tools、rejected/expired→END、pending→再等。
    """
    bound = model.bind_tools(tools)  # 绑定工具
    tool_node = ToolNode(tools)  # 执行节点

    # —— agent：调模型（系统提示 + 历史），支持逐 token 流式透传 ——
    async def agent_node(state: ReactState) -> dict[str, Any]:  # 模型节点
        history = list(state.get("messages") or [])  # 历史
        messages = [SystemMessage(content=system_prompt), *history]  # 组装入参
        if token_sink is not None:  # 接了流式回调 → 用 astream 逐块透传
            parts: list[str] = []  # 文本块
            last: Any = None  # 最后一个块（含聚合 tool_calls/id）
            resp: AIMessage  # 收集到完整结果
            try:  # 模型不支持流式时降级成 ainvoke（只包迭代本身，失败构造不算）
                incomplete = False  # 是否流式中断
                async for chunk in _astream_retry(bound, messages):  # 逐块（首 token 前按类型重试 + 发 RETRY 事件）
                    last = chunk  # 记住最新
                    text = _chunk_text(chunk)  # 取纯文本
                    if text:  # 只有文本块才往外发（工具调用块无正文，不产生 DELTA）
                        parts.append(text)  # 收集
                        token_sink(text)  # 逐 token 透传给 UI/partial_answer
            except Exception:  # 流式启动/中断失败
                incomplete = True  # 标记，改走非流式
            if incomplete:  # 流式不可用
                resp = await bound.ainvoke(messages)  # 回退非流式（不丢调用）
            else:  # 流式成功 → 重建完整助手消息
                tcs = None  # 聚合工具调用
                try:  # AIMessageChunk.tool_calls 可能是属性，安全取值
                    tcs = getattr(last, "tool_calls", None) or None  # 聚合调用
                except Exception:  # 取不到
                    tcs = None  # 视为无
                message_kwargs: dict[str, Any] = {  # 重建助手消息的公共参数
                    "content": "".join(parts),  # 汇聚后的正文
                    "id": getattr(last, "id", None),  # 保留 id
                }  # 结束
                if tcs:  # 有工具调用才传（AIMessage.tool_calls 必须为 list，None 会校验失败）
                    message_kwargs["tool_calls"] = tcs  # 供后续路由
                resp = AIMessage(**message_kwargs)  # 重建完整助手消息（block 不能当 AIMessage 用）
        else:  # 没接流式 → 原样非流式
            resp = await bound.ainvoke(messages)  # 一次性
        return {"messages": [resp], "status": "running"}  # 返回

    # —— approval_gate：该不该审；审则拦截该调用 + 落表(best-effort) ——
    def approval_gate(state: ReactState) -> dict[str, Any]:  # 审批门
        if state.get("approval_checked"):  # 本轮已判过
            return {"approval_checked": False}  # 复位，下一轮再判
        call = (state.get("messages") or [])[-1]  # 末尾模型（带 tool_calls）
        need: list[Any] = []  # 需审批的调用
        fused: list[Any] = []  # 已熔断、本次拦截不执行的调用
        blocked_names = _circuit_blocked_names(state)  # 已熔断的工具名
        for tc in getattr(call, "tool_calls", None) or []:  # 逐个调用
            tool = next((t for t in tools if t.name == tc.get("name")), None)  # 找工具
            if tool is None:  # 未知工具
                continue  # 跳过
            if str(tc.get("name") or "") in blocked_names:  # 该工具已熔断
                fused.append(tc)  # 拦下不再执行
                continue  # 不往下判审批
            want, _reason = should_require_approval(describe(tool), args=tc.get("args") or {})  # 判定
            if want:  # 需审
                need.append(tc)  # 记
        if fused:  # 有熔断拦截：回模型说明，本轮不执行这些工具
            fused_msgs: list[Any] = []  # 熔断消息
            for tc in fused:  # 逐调用
                fused_msgs.append(  # 生成 ToolMessage（不执行）
                    ToolMessage(
                        content=f"工具 {tc.get('name')} 已连续失败多次，已熔断暂停使用；请换一种方式或说明结果。",
                        tool_call_id=str(tc.get("id") or ""),
                        name=str(tc.get("name") or ""),
                    )
                )  # 追加
            return {"messages": fused_msgs, "approval_checked": True, "status": "running"}  # 回模型
        if not need:  # 无审批需求 → 直通 tools
            return {"approval_checked": True}  # 放行
        # 需审批：先落表(best-effort)，再按模式阻断或暂停
        import time as _time  # 时间

        ids: list[str] = []  # 请求 id
        try:  # 落表失败不阻断
            from app.agent.approval import repository as approval_repo  # 审批仓储

            for tc in need:  # 逐调用
                ids.append(approval_repo.create_request(  # 落一条 pending 并取 id
                    thread_id=str(state.get("thread_id") or ""),  # 线程
                    tenant_id=str(state.get("tenant_id") or "default"),  # 租户
                    requester_id=str(state.get("requester_id") or "user"),  # 发起
                    tool_name=str(tc.get("name") or ""),  # 工具
                    # args_redacted 存执行所需的真实参数（批准后 execute_approved 用它真跑）；
                    # 对外的展示/审计需脱敏时由 app.agent.gateway.redact 在读侧处理。
                    args_redacted=dict(tc.get("args") or {}),  # 参数
                    resource_summary=f"需要审批：{tc.get('name')}",  # 摘要
                    mutation="read_only",  # 变更画像
                    risk="medium",  # 风险
                    approval_reason="工具策略要求人工审批",  # 原因
                    expires_at=_time.time() + 3600,  # 1 小时
                ))  # 结束
                _approval_stats["created"] += 1  # 计登记
        except Exception:  # 未配置库等
            pass  # 仍照常走

        if not (checkpointer is not None and interrupt_on_approval):  # 非恢复模式：拦截不执行，但把待审上抛
            _approval_stats["blocked"] += len(need)  # 计拦截
            pending_meta: list[Any] = []  # 上抛元信息
            for rid, tc in zip(ids, need):  # 逐调用
                pending_meta.append({  # 元信息
                    "request_id": str(rid),  # 请求 id
                    "tool_name": str(tc.get("name") or ""),  # 工具
                    "summary": _approval_summary(str(tc.get("name") or ""), tc.get("args") or {}),  # 待确认内容
                })  # 追加
            blocked: list[Any] = []  # 拦截消息
            for tc in need:  # 逐调用
                blocked.append(  # 生成 ToolMessage（不执行该工具）
                    ToolMessage(
                        content=f"工具 {tc.get('name')} 需要人工审批，已登记待审，本次不执行。",
                        tool_call_id=str(tc.get("id") or ""),
                        name=str(tc.get("name") or ""),
                    )
                )  # 追加
            return {  # 回模型
                "messages": blocked,  # 拦截消息
                "approval_checked": True,  # 已判
                "approval_blocked": True,  # 已拦截
                "approval_pending": pending_meta,  # 上抛待审（路由级弹框用）
                "status": "running",  # 运行态
            }  # 结束

        # 恢复模式：interrupt 暂停等待人工决策；恢复后读表定走向
        interrupt({"request_ids": ids})  # 暂停（恢复时此处返回 resume 值）

        def _status(rid: str) -> str:  # 读审批状态
            try:  # 读表
                from app.agent.approval import repository as repo  # 仓储

                row = repo.get_request(rid)  # 取
                return str((row or {}).get("status") or "pending")  # 状态
            except Exception:  # 读失败
                return "pending"  # 视作未决

        statuses = [_status(r) for r in ids]  # 全部状态
        if any(s == "rejected" for s in statuses):  # 有拒绝
            return {"status": "rejected"}  # 停
        if any(s == "expired" for s in statuses):  # 有过期
            return {"status": "expired"}  # 停
        if any(s == "pending" for s in statuses):  # 仍未决
            interrupt({"request_ids": ids})  # 再等一轮
        return {"approval_checked": True}  # 已批 → 放行执行

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
    return g.compile(checkpointer=checkpointer)  # 编译（恢复模式需 checkpointer）