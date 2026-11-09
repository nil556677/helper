"""驱动器：驱动图 + 事件收敛（stream 与 runtime 共用）。

职责：
- 开 trace 会话、回执 ack、启动前取消短路；
- 调 `_CHAIN.before/after_run`、绑定 `run_context`、`graph.astream` 观测节点完成；
- `_run_graph` 用投影把节点完成驱动成状态机迁移（节点不再自行 transition）；
- 收敛异常：用户取消 / 任务取消 / 递归超限 / 其他失败 → 统一落部分结果 + 发终态；
- `recursion_limit` 由这里显式传入（超限收敛为 `failed`）。

抽出来后，`stream._run` 只剩「建初始状态 + 收尾出口」，差别只在出口（队列 vs Turn）。
"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 队列/取消
import contextlib  # 抑制收尾异常
import json  # 元数据序列化
from dataclasses import dataclass  # 数据类
from typing import Any  # 任意值

from langgraph.errors import GraphRecursionError  # 递归超限
from langgraph.types import Command  # HITL 恢复命令
from loguru import logger  # 日志（失败要能查堆栈）

from app.agent import events  # 事件
from app.agent.context import RunCancelled, RunContext, bind_run, run_context, unbind_run  # 上下文
from app.agent.fsm.projection import project_event  # 节点完成 → 迁移事件
from app.agent.fsm.states import RunEvent, RunState  # 状态
from app.agent.middleware import default_chain  # 中间件
from app.core.trace import store as trace_store  # trace 写库
from app.core.trace import writer as trace_writer  # trace 异步写
from app.core.trace.recorder import trace_run  # trace 会话

#: 图步硬上限（暂定 12，可配置）：防 agent↔工具无限循环。
RECURSION_LIMIT = 12

_CHAIN = default_chain()  # 中间件链


@dataclass
class DriverResult:  # 驱动器结果
    """最终状态 + 状态机终态 + 可选错误（批处理入口用）。"""

    state: dict[str, Any]  # 图最终状态（成功时有内容）
    status: str  # done / cancelled / failed
    error: BaseException | None = None  # 失败时的异常


def _mark_cancelled(ctx: RunContext) -> None:  # 收敛到取消终态
    """无论从哪个活跃态取消，都走到 CANCELLED。"""
    if ctx.machine.state is RunState.CANCELLED:  # 已经是终态
        return  # 幂等
    if ctx.machine.can(RunEvent.CANCEL_REQUEST):  # 还能请求
        ctx.transition(RunEvent.CANCEL_REQUEST)  # 进入 CANCELLING
    if ctx.machine.can(RunEvent.CANCEL_ACK):  # 还能确认
        ctx.transition(RunEvent.CANCEL_ACK)  # 进入 CANCELLED


async def _persist(ctx: RunContext, question: str, answer: str) -> None:  # 落库（异步优先）
    """取消/失败也落一轮；ctx 挂了 AsyncDB 就异步落库（单一异步写者），否则同步。"""
    from app.agent.persist import persist_turn, persist_turn_async  # 落库

    if getattr(ctx, "db", None) is not None:  # 有异步连接
        await persist_turn_async(  # 异步
            ctx,
            conversation_id=ctx.conversation_id,
            question=question,
            answer=answer,
            note="",
            fragments=[],
        )  # 结束
    else:  # 同步回退
        persist_turn(
            ctx,
            conversation_id=ctx.conversation_id,
            question=question,
            answer=answer,
            note="",
            fragments=[],
        )  # 结束


def _metrics_of(ctx: RunContext, state: dict[str, Any]) -> dict[str, Any]:  # 指标汇总
    """收尾把本 run 指标补齐派生值并落 metadata（单一来源）。"""
    from app.agent.metrics import finalize  # 局部导入避免循环
    from app.agent.tools.tooling import singleflight_timeouts  # 进程级超时计数

    metrics = finalize(ctx.metrics)  # 计数器 + 派生
    metrics["singleflight_timeouts"] = max(0, singleflight_timeouts() - int(getattr(ctx, "singleflight_base", 0)))  # 本 run 内等待超时次数
    metrics["question_chars"] = len(str(ctx.question or ""))  # 问题体积
    metrics["answer_chars"] = len(str(state.get("answer") or ""))  # 回答体积
    metrics["ttft_ms"] = int(ctx.first_token_ms or 0)  # 首 token
    return metrics  # 返回


def _tools_summary(ctx: RunContext) -> dict[str, Any]:  # 工具调用汇总
    """一次运行的工具调用聚合：次数 / 总耗时 / 缓存命中 / 出错次数。"""
    calls = list(getattr(ctx, "tool_calls", None) or [])  # 逐条调用
    return {  # 汇总
        "count": len(calls),  # 次数
        "total_ms": sum(int(item.get("ms") or 0) for item in calls),  # 总耗时
        "hits_cached": sum(1 for item in calls if item.get("cached")),  # 缓存命中
        "errors": sum(1 for item in calls if not item.get("ok")),  # 出错次数
    }  # 返回


def _decision_chain(state: dict[str, Any]) -> dict[str, Any]:  # 决策链(问题→输出)
    """把路由/分诊/校验等决策快照收进 metadata，供排障回看。"""
    keys = ("preprocessed", "route", "semantic_recall", "intent", "triage", "verify", "clarify_options")  # 决策通道
    return {key: state.get(key) for key in keys if state.get(key)}  # 只收有值的


def _route_json(state: dict[str, Any]) -> str:  # 真实路由 JSON(不再写死 rag)
    """取 RouteDecision/Triage 的可读快照；都没有则回退 none。"""
    route = state.get("route")  # 决策对象
    if isinstance(route, dict) and route:  # 有路由
        return json.dumps({  # 快照
            "mode": route.get("decided_by") or "route",  # 谁定的
            "reason": route.get("reason") or "",  # 理由
            "route": route.get("route") or "",  # 路径
        }, ensure_ascii=False)  # 中文保留
    triage = state.get("triage")  # 分诊对象
    if isinstance(triage, dict) and triage:  # 有分诊
        return json.dumps({  # 快照
            "mode": "triage",  # 分诊
            "reason": str(triage.get("escalate_reason") or triage.get("path_type") or ""),  # 原因/路径
            "route": triage.get("agent") or "",  # 专家
        }, ensure_ascii=False)  # 中文保留
    return '{"mode": "none"}'  # 都没有 → 未知


def _update_trace(ctx: RunContext, state: dict[str, Any]) -> None:  # 补 trace 摘要
    """补 run 级汇总：route/answer/model/TTFT + 元数据；优先读最终 state（E3）。"""
    with contextlib.suppress(Exception):  # trace 失败不影响
        usage = dict(state.get("usage") or {})  # 用量（只读 state）
        gate = dict(state.get("gate") or {})  # 门限（只读 state）
        basis = dict(state.get("basis") or {})  # 依据（只读 state）
        status = str(state.get("status") or ctx.machine.state.value)  # 终态
        metadata = {  # 元数据（E 的对账会用）
            "turn_id": str(state.get("turn_id") or ctx.turn_id),  # 轮次 id
            "status": status,  # 运行状态
            "attempt": int(state.get("attempt") or 1),  # 尝试次数
            "usage": usage,  # 用量
            "gate": gate,  # 门限判决
            "basis": basis,  # 检索依据
            "tools": _tools_summary(ctx),  # 工具调用汇总（可观测）
            "metrics": _metrics_of(ctx, state),  # 指标汇总（可观测）
            "decisions": _decision_chain(state),  # 决策链（排障）
            "steps": list(ctx.step_records),  # 逐步骤（节点做了什么）
        }  # 元数据结束
        trace_store.update_run(  # 写
            ctx.run_id,  # run
            route_json=_route_json(state),  # 真实路由
            answer=str(state.get("answer") or ctx.partial_answer),  # 已生成
            model="llm",  # 模型槽位
            ttft_ms=ctx.first_token_ms or 0,  # 首 token
            metadata_json=json.dumps(metadata, ensure_ascii=False),  # 元数据
        )  # 写结束


async def _update_trace_async(ctx: RunContext, state: dict[str, Any]) -> None:  # 补 trace（异步）
    """与 ``_update_trace`` 同内容，经 ``ctx.db`` 异步写（单一异步写者）。"""
    with contextlib.suppress(Exception):  # trace 失败不影响
        usage = dict(state.get("usage") or {})  # 用量（只读 state）
        gate = dict(state.get("gate") or {})  # 门限（只读 state）
        basis = dict(state.get("basis") or {})  # 依据（只读 state）
        status = str(state.get("status") or ctx.machine.state.value)  # 终态
        metadata = {  # 元数据
            "turn_id": str(state.get("turn_id") or ctx.turn_id),  # 轮次 id
            "status": status,  # 状态
            "attempt": int(state.get("attempt") or 1),  # 尝试
            "usage": usage,  # 用量
            "gate": gate,  # 门限
            "basis": basis,  # 依据
            "decisions": _decision_chain(state),  # 决策链（排障）
            "steps": list(ctx.step_records),  # 逐步骤（节点做了什么）
        }  # 结束
        from app.core.trace import store as _store  # 异步写

        await _store.update_run_async(  # 异步写
            ctx.db,  # 异步连接
            ctx.run_id,  # run
            route_json=_route_json(state),  # 真实路由
            answer=str(state.get("answer") or ctx.partial_answer),  # 已生成
            model="llm",  # 模型槽位
            ttft_ms=ctx.first_token_ms or 0,  # 首 token
            metadata_json=json.dumps(metadata, ensure_ascii=False),  # 元数据
        )  # 写结束


def _seal_state(ctx: RunContext, state: dict[str, Any], status: str) -> None:  # 封终态进 state
    """只补 driver 才知道的终态与轮次身份；业务字段由节点返回进 state（单一来源）。"""
    for key, value in {  # 仅三项
        "turn_id": ctx.turn_id,  # 轮次 id（落库后回填）
        "status": status,  # 终态（done/cancelled/failed）
        "attempt": int(state.get("attempt") or 1),  # 尝试次数（缺省 1）
    }.items():  # 逐项
        state.setdefault(key, value)  # 只补缺的


def _sink_run_id(emit: Any, run_id: str) -> None:  # 回填 run id
    """把真实 run id 回填给事件出口（`_EventSink`），让缺 id 的事件都盖上章。"""
    if hasattr(emit, "_run_id"):  # 是 _EventSink
        emit._run_id = run_id  # 回填


async def _run_graph(  # 跑父图并驱动状态机
    graph: Any,  # 编译父图
    graph_input: Any,  # 输入（普通 state 或 Command(resume)）
    ctx: RunContext,  # 运行上下文
    emit: Any,  # 事件出口
) -> dict[str, Any]:  # 权威最终状态
    """用 astream 观测节点完成，driver 统一驱动状态迁移（节点不再自行 transition）。

    每条 ``updates`` 记录都是「节点名 → 该节点返回的更新」，driver 用投影把完成的节点
    映射成 ``RunEvent`` 并执行迁移（发 state 事件）；``values`` 是每一步的完整状态快照，
    取最后一条作为权威最终状态（reducer 已合并，避免手拼合并漏字段）。
    """
    config = {  # 图配置
        "configurable": {  # configurable
            "run_ctx": ctx,  # 上下文
            "thread_id": ctx.conversation_id,  # checkpoint 线程 = 会话
        },  # configurable 结束
        "metadata": {  # checkpoint 元数据（对账定位）
            "turn_id": ctx.turn_id,  # 轮次
            "attempt": int(graph_input.get("attempt") or 1) if isinstance(graph_input, dict) else 1,  # 尝试次数
        },  # 元数据结束
        "recursion_limit": RECURSION_LIMIT,  # 图步上限
    }  # config 结束
    final_state: dict[str, Any] = {}  # 权威最终状态
    async for mode, chunk in graph.astream(  # 流式观测（双模式）
        graph_input,  # 输入
        config,  # 配置
        stream_mode=["updates", "values"],  # 更新 + 完整状态快照
    ):  # 双模式开始
        if mode == "updates":  # 节点完成记录
            if "__interrupt__" in chunk:  # HITL 暂停（澄清/审批）
                items = chunk.get("__interrupt__") or ()  # 中断列表
                value = getattr(items[0], "value", None) if items else None  # 首个负载
                ctx.interrupt_payload = value if isinstance(value, dict) else {"value": value}  # 记录待办
                if ctx.machine.can(RunEvent.INTERRUPT):  # 可迁移
                    ctx.transition(RunEvent.INTERRUPT, reason="hitl")  # 进入 WAITING
                payload = ctx.interrupt_payload or {}  # 取负载
                emit(  # 发给前端渲染（等待用户选择）
                    events.clarify(  # 澄清待办
                        str(payload.get("thread_id") or ctx.conversation_id),  # 线程
                        str(payload.get("title") or "需要确认"),  # 标题
                        str(payload.get("description") or ""),  # 说明
                        list(payload.get("options") or []),  # 选项
                        str(payload.get("risk") or "low"),  # 风险
                        str(payload.get("kind") or "choose"),  # 选路由/补参数
                    )  # 事件结束
                )  # emit 结束
                break  # 停止消费（图已暂停）
            for node, update in chunk.items():  # 逐个完成的节点
                if "__interrupt__" == node:  # 防御：跳过暂停伪节点
                    continue  # 下一个
                ctx.step_records.append({"node": node, "update": json.dumps(update, ensure_ascii=False)[:600]})  # 记该步做了什么
                event = project_event(node, update)  # 节点 → 迁移事件
                if event is not None and ctx.machine.can(event):  # 有事件且当前状态允许
                    ctx.transition(event)  # driver 统一驱动迁移（发 state 事件）
        else:  # values：完整状态快照
            final_state = dict(chunk)  # 收敛为最新（reducer 已合并）
    # 非流式图（dispatch/general/兜底）不产出 DELTA：收尾前把最终答案/引用/说明补发给前端
    answer = str(final_state.get("answer") or "")  # 最终答案
    if ctx.machine.state is RunState.DONE and answer and not ctx.partial_answer:  # 没流过字才补
        emit(events.replace(answer))  # 整段下发（UI 会替换正文区）
        ctx.partial_answer = answer  # 记为已产出（落库/对账共用）
        fragments = [item for item in (final_state.get("fragments") or []) if isinstance(item, dict)]  # 引用
        if fragments:  # 有引用
            emit(events.citation(fragments))  # 引用事件
        note = str(final_state.get("note") or "")  # 说明
        if note:  # 有说明
            emit(events.note(note))  # 说明事件
    if ctx.machine.state is RunState.DONE:  # 正常走到 done（finalize 已驱动迁移）
        emit(  # 发完成终态（最后一帧，顺序在 state 事件之后）
            events.done(  # 完成事件
                ctx.first_token_ms or 0,  # 首 token
                ctx.elapsed_ms,  # 总耗时
                dict(final_state.get("usage") or {}),  # 用量（读 state）
            )  # 事件结束
        )  # emit 结束
    return final_state  # 返回权威最终状态


async def drive(  # 驱动图
    graph: Any,  # 编译父图
    ctx: RunContext,  # 运行上下文
    *,  # 仅关键字
    initial_state: dict[str, Any],  # 初始状态
    emit: Any | None = None,  # 事件出口（默认用 ctx.emit）
    resume_value: Any = None,  # HITL 恢复值（非 None 时用 Command(resume) 继续）
) -> DriverResult:  # 结果
    """驱动图跑到终态；ctx.db 存在则 trace 走 async（单一异步写者），否则同步。"""
    emit = emit if emit is not None else ctx.emit  # 事件出口
    question = ctx.question  # 问题
    state: dict[str, Any] = {}  # 图最终状态
    status = "failed"  # 终态，先按失败
    error: BaseException | None = None  # 可选错误
    resuming = resume_value is not None  # 是否恢复
    graph_input: Any = Command(resume=resume_value) if resuming else initial_state  # 输入

    async def _body(session: Any) -> DriverResult:  # 执行体（sync/async trace 共用）
        nonlocal state, status, error  # 改外层
        ctx.run_id = session.run_id  # 对齐 id
        _sink_run_id(emit, ctx.run_id)  # 回填事件出口
        emit(events.ack(ctx.run_id))  # 先回执
        if ctx.cancel_event.is_set():  # 启动前就取消
            session.set_status("cancelled")  # trace
            if ctx.machine.can(RunEvent.CANCEL_REQUEST):  # 暂停/活跃态可请求取消
                ctx.transition(RunEvent.CANCEL_REQUEST, reason="user")  # 取消中
            if ctx.machine.can(RunEvent.CANCEL_ACK):  # 再确认完成
                ctx.transition(RunEvent.CANCEL_ACK, reason="user")  # 已取消
            emit(events.cancelled("user"))  # 事件
            return DriverResult({}, "cancelled")  # 短路
        try:  # 图执行
            if resuming and ctx.machine.can(RunEvent.RESUME):  # 恢复
                ctx.transition(RunEvent.RESUME)  # WAITING → CONTEXT_LOADING
            else:  # 新跑
                ctx.transition(RunEvent.START)  # 进入上下文
            await _CHAIN.before_run(ctx)  # 中间件（如取消边界）
            bind_run(ctx.conversation_id, ctx)  # 注册：节点按 thread_id 取回 run
            with run_context(ctx):  # 绑定当前 run
                state = await _run_graph(  # 跑父图 + 驱动状态机
                    graph,  # 编译父图
                    graph_input,  # 输入（state 或 Command）
                    ctx,  # 上下文（投影用）
                    emit,  # 事件出口（driver 收权后发 state/done）
                )  # 图结束
            unbind_run(ctx.conversation_id)  # 释放注册
            if ctx.machine.state is RunState.WAITING:  # HITL 暂停
                status = "waiting"  # 非终态（等用户）
                _seal_state(ctx, state, status)  # 封业务字段
            else:  # 正常
                status = "done"  # 正常结束
                _seal_state(ctx, state, status)  # 封业务字段进最终 state
        except RunCancelled:  # 用户取消
            session.set_status("cancelled")  # trace
            _mark_cancelled(ctx)  # 收敛状态
            await _persist(ctx, question, ctx.partial_answer)  # 保存部分
            emit(events.cancelled("user"))  # 事件
            status = "cancelled"  # 终态
        except asyncio.CancelledError:  # 任务被取消
            session.set_status("cancelled")  # trace
            _mark_cancelled(ctx)  # 收敛状态
            await _persist(ctx, question, ctx.partial_answer)  # 保存部分
            emit(events.cancelled("task"))  # 事件
            status = "cancelled"  # 终态
        except GraphRecursionError as exc:  # 工具循环超限
            logger.opt(exception=exc).warning("run 递归超限（recursion_limit={}）", RECURSION_LIMIT)  # 记堆栈
            session.set_status("error")  # trace
            if ctx.machine.can(RunEvent.FAIL):  # 可迁移
                ctx.transition(RunEvent.FAIL, reason="recursion_limit")  # 失败
            emit(events.error("recursion_limit", "工具循环超过上限，已停止。", "run"))  # 事件
            await _persist(ctx, question, ctx.partial_answer)  # 保存部分
            status = "failed"  # 终态
            error = exc  # 错误
        except Exception as exc:  # 其他失败
            logger.opt(exception=exc).error("run 执行失败：{}", type(exc).__name__)  # 记堆栈
            session.set_status("error")  # trace
            if ctx.machine.can(RunEvent.FAIL):  # 可迁移
                ctx.transition(RunEvent.FAIL, reason=str(exc)[:120])  # 失败
            emit(events.error("run_failed", str(exc)[:300], "run"))  # 事件
            await _persist(ctx, question, ctx.partial_answer)  # 保存部分
            status = "failed"  # 终态
            error = exc  # 错误
        finally:  # 中间件收尾
            with contextlib.suppress(Exception):  # 收尾失败不阻断
                await _CHAIN.after_run(ctx)  # 逆序
            unbind_run(ctx.conversation_id)  # 一定释放 run 注册（含异常路径）
            if getattr(ctx, "db", None) is not None:  # 有异步连接
                await _update_trace_async(ctx, state)  # 异步补 trace
            else:  # 同步
                _update_trace(ctx, state)  # 同步补 trace
            await trace_writer.flush()  # 排空异步 span
        return DriverResult(state=state, status=status, error=error)  # 返回

    try:  # 外层兜底（连 trace 都起不来）
        if getattr(ctx, "db", None) is not None:  # 有异步连接 → 异步 trace
            from app.core.trace.recorder import trace_run_async  # 异步会话

            async with trace_run_async(  # 异步 trace
                ctx.db, question=question, conversation_id=ctx.conversation_id,
                run_id=ctx.run_id or None,  # 复用发送即落库的 run_id
            ) as session:  # 会话
                return await _body(session)  # 执行
        with trace_run(  # 同步 trace
            question=question, conversation_id=ctx.conversation_id,
            run_id=ctx.run_id or None,  # 复用发送即落库的 run_id
        ) as session:  # 会话
            return await _body(session)  # 执行
    except Exception as exc:  # 连 trace/链都起不来
        emit(events.error("bootstrap_failed", str(exc)[:300], "run"))  # 事件
        error = exc  # 记录
    return DriverResult(state=state, status=status, error=error)  # 返回
