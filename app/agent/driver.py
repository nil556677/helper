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
from loguru import logger  # 日志（失败要能查堆栈）

from app.agent import events  # 事件
from app.agent.context import RunCancelled, RunContext, run_context  # 上下文
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
            note=ctx.note,
            fragments=ctx.citations,
        )  # 结束
    else:  # 同步回退
        persist_turn(
            ctx,
            conversation_id=ctx.conversation_id,
            question=question,
            answer=answer,
            note=ctx.note,
            fragments=ctx.citations,
        )  # 结束


def _update_trace(ctx: RunContext, state: dict[str, Any]) -> None:  # 补 trace 摘要
    """补 run 级汇总：route/answer/model/TTFT + 元数据；优先读最终 state（E3）。"""
    with contextlib.suppress(Exception):  # trace 失败不影响
        usage = dict(state.get("usage") or ctx.usage)  # 用量（state 优先）
        gate = dict(state.get("gate") or ctx.gate)  # 门限
        basis = dict(state.get("basis") or ctx.basis)  # 依据
        status = str(state.get("status") or ctx.machine.state.value)  # 终态
        metadata = {  # 元数据（E 的对账会用）
            "turn_id": str(state.get("turn_id") or ctx.turn_id),  # 轮次 id
            "status": status,  # 运行状态
            "attempt": int(state.get("attempt") or ctx.attempt),  # 尝试次数
            "usage": usage,  # 用量
            "gate": gate,  # 门限判决
            "basis": basis,  # 检索依据
        }  # 元数据结束
        trace_store.update_run(  # 写
            ctx.run_id,  # run
            route_json='{"mode": "rag"}',  # 固定 RAG
            answer=str(state.get("answer") or ctx.partial_answer),  # 已生成
            model="llm",  # 模型槽位
            ttft_ms=ctx.first_token_ms or 0,  # 首 token
            metadata_json=json.dumps(metadata, ensure_ascii=False),  # 元数据
        )  # 写结束


def _seal_state(ctx: RunContext, state: dict[str, Any], status: str) -> None:  # 封业务字段进 state
    """把 ctx 里还没进 state 的业务字段补进最终 state（E3：state 为最终快照）。

    单一出口——图结束后在这里对齐一次，避免各节点残留的双真相漏到 checkpoint/对账。
    """
    fields = {  # 从 ctx 取 → state 的映射
        "usage": dict(ctx.usage),  # 用量
        "settings": dict(ctx.settings),  # 设置
        "basis": dict(ctx.basis),  # 依据
        "gate": dict(ctx.gate),  # 门限
        "reasoning": ctx.reasoning,  # 思考
        "conflict": ctx.conflict,  # 冲突
        "turn_id": ctx.turn_id,  # 轮次 id
        "status": status,  # driver 终态（done/cancelled/failed）
        "attempt": int(ctx.attempt),  # 尝试次数
        "steps": list(ctx.steps),  # 过程步骤
    }  # 映射结束
    for key, value in fields.items():  # 逐字段
        state.setdefault(key, value)  # 只补缺的
    state.setdefault("answer", ctx.partial_answer)  # 兜底答案
    state.setdefault("note", ctx.note)  # 兜底说明


def _sink_run_id(emit: Any, run_id: str) -> None:  # 回填 run id
    """把真实 run id 回填给事件出口（`_EventSink`），让缺 id 的事件都盖上章。"""
    if hasattr(emit, "_run_id"):  # 是 _EventSink
        emit._run_id = run_id  # 回填


async def _run_graph(  # 跑父图并驱动状态机
    graph: Any,  # 编译父图
    initial_state: dict[str, Any],  # 初始状态
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
            "attempt": int(ctx.attempt),  # 尝试次数
        },  # 元数据结束
        "recursion_limit": RECURSION_LIMIT,  # 图步上限
    }  # config 结束
    final_state: dict[str, Any] = {}  # 权威最终状态
    async for mode, chunk in graph.astream(  # 流式观测（双模式）
        initial_state,  # 初始状态
        config,  # 配置
        stream_mode=["updates", "values"],  # 更新 + 完整状态快照
    ):  # 双模式开始
        if mode == "updates":  # 节点完成记录
            for node, update in chunk.items():  # 逐个完成的节点
                event = project_event(node, update)  # 节点 → 迁移事件
                if event is not None and ctx.machine.can(event):  # 有事件且当前状态允许
                    ctx.transition(event)  # driver 统一驱动迁移（发 state 事件）
        else:  # values：完整状态快照
            final_state = dict(chunk)  # 收敛为最新（reducer 已合并）
    if ctx.machine.state is RunState.DONE:  # 正常走到 done（finalize 已驱动迁移）
        emit(  # 发完成终态（最后一帧，顺序在 state 事件之后）
            events.done(  # 完成事件
                ctx.first_token_ms or 0,  # 首 token
                ctx.elapsed_ms,  # 总耗时
                ctx.usage,  # 用量
            )  # 事件结束
        )  # emit 结束
    return final_state  # 返回权威最终状态


async def drive(  # 驱动图
    graph: Any,  # 编译父图
    ctx: RunContext,  # 运行上下文
    *,  # 仅关键字
    initial_state: dict[str, Any],  # 初始状态
    emit: Any | None = None,  # 事件出口（默认用 ctx.emit）
) -> DriverResult:  # 结果
    """驱动图跑到终态；任何路径都收敛、落部分、发终态。"""
    emit = emit if emit is not None else ctx.emit  # 事件出口
    question = ctx.question  # 问题
    state: dict[str, Any] = {}  # 图最终状态
    status = "failed"  # 终态，先按失败
    error: BaseException | None = None  # 可选错误
    try:  # 外层兜底（连 trace 都起不来）
        with trace_run(question=question, conversation_id=ctx.conversation_id) as session:  # trace
            ctx.run_id = session.run_id  # 对齐 id
            _sink_run_id(emit, ctx.run_id)  # 回填事件出口
            emit(events.ack(ctx.run_id))  # 先回执
            if ctx.cancel_event.is_set():  # 启动前就取消
                session.set_status("cancelled")  # trace
                emit(events.cancelled("user"))  # 事件
                return DriverResult({}, "cancelled")  # 短路
            try:  # 图执行
                ctx.transition(RunEvent.START)  # 进入上下文
                await _CHAIN.before_run(ctx)  # 中间件（如取消边界）
                with run_context(ctx):  # 绑定当前 run
                    state = await _run_graph(  # 跑父图 + 驱动状态机
                        graph,  # 编译父图
                        initial_state,  # 初始状态
                        ctx,  # 上下文（投影用）
                        emit,  # 事件出口（driver 收权后发 state/done）
                    )  # 图结束
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
                logger.opt(exception=exc).error("run 执行失败：{}", type(exc).__name__)  # 记堆栈（排障关键）
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
                _update_trace(ctx, state)  # 补 trace（读最终 state）
                await trace_writer.flush()  # 排空异步 span，保证 run 收尾前落库
    except Exception as exc:  # 连 trace/链都起不来
        emit(events.error("bootstrap_failed", str(exc)[:300], "run"))  # 事件
        error = exc  # 记录
    return DriverResult(state=state, status=status, error=error)  # 返回
