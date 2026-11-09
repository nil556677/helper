"""流式 run：图在后台跑，事件通过队列给 UI。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 队列/任务
import uuid  # run id
from dataclasses import dataclass, field  # 数据类
from typing import Any  # 任意值

from app.agent import (
    assembly,  # 装配器（建图）
    events,  # 事件
)
from app.agent.context import RunContext  # 上下文
from app.agent.driver import drive  # 驱动器（图驱动 + 终态收敛）
from app.agent.fsm.states import RunEvent  # 状态事件（暂停态取消）
from app.services import attachments  # 附件公开字段

SENTINEL = object()  # 队列结束标记

#: 是否让 `_run` 自动为每轮开 AsyncDB（单一异步写者）。
#: 默认 **False**：pytest 多事件循环下开连接会挂，测试走同步回退；
#: 真实 worker 由 ``worker_cp.setup_loop`` 置 True（单事件循环，安全）。
ASYNC_DB_ENABLED = False

#: 终态事件集合：一个 run 只允许出现一次，且必须是最后一帧（契约层定义）。
_TERMINAL = events.TERMINAL_TYPES  # 终态集合


class _EventSink:  # 事件出口
    """把事件塞进队列，并在终态之后自动闭嘴。

    没有这层时，「done 已经发了，收尾又抛异常再发 error」会让界面同时收到两个结尾，
    状态机就会停在 error 上——用户看到的是回答已完整、却标着失败。
    """

    def __init__(self, queue: asyncio.Queue) -> None:  # 构造
        self._queue = queue  # 目标队列
        self._closed = False  # 是否已发过终态
        self._run_id = ""  # 归属 run（start_stream / drive 回填）

    def __call__(self, event: events.EventEnvelope) -> None:  # 发一个事件
        """终态之后的事件直接丢弃；顺手给缺 run_id 的事件盖章。"""
        if self._closed:  # 已经结束
            return  # 丢掉
        if not event.run_id and self._run_id:  # 没有 run_id 且有可用的
            event = event.model_copy(update={"run_id": self._run_id})  # 盖章
        if event.type in _TERMINAL:  # 这一条是终态
            self._closed = True  # 关门
        self._queue.put_nowait(event)  # 入队

    @property
    def closed(self) -> bool:  # 是否已结束
        """给测试/收尾判断用。"""
        return self._closed  # 返回


@dataclass
class StreamHandle:  # UI 拿到的句柄
    """events 由 UI 消费；cancel() 只置取消信号。"""

    run_id: str  # 运行 id
    ctx: RunContext  # 上下文
    events: asyncio.Queue = field(default_factory=asyncio.Queue)  # 事件队列
    task: asyncio.Task | None = None  # 后台任务
    approvals: dict[str, asyncio.Future] = field(default_factory=dict)  # 等待审批
    started: bool = False  # 后台任务是否已开始执行（取消时判断能不能 cancel 任务）
    graph: Any = None  # 被驱动的图（HITL 恢复用）
    thread_id: str = ""  # 恢复用的线程 id（同 thread_id）
    paused: bool = False  # 是否停在 HITL 暂停（等待恢复）

    async def request_approval(self, request_id: str, payload: dict[str, Any]) -> bool:  # 请求审批
        """发 approval 事件并等待 UI 决定；默认为拒绝。"""
        future: asyncio.Future = asyncio.get_running_loop().create_future()  # future
        self.approvals[request_id] = future  # 记录
        self.events.put_nowait(events.approval(request_id, **payload))  # 事件
        try:  # 等
            return bool(await future)  # 结果
        finally:  # 清理
            self.approvals.pop(request_id, None)  # 移除

    def approve(self, request_id: str, allow: bool) -> None:  # UI 决定
        """允许/拒绝。"""
        future = self.approvals.get(request_id)  # future
        if future is not None and not future.done():  # 等待中
            future.set_result(allow)  # 唤醒

    def cancel(self) -> None:  # 停止
        """信号式取消：运行中取消任务；暂停态直接收敛为已取消（专门出口）。"""
        self.ctx.request_cancel()  # 置位（启动前取消靠它）
        if self.started and self.task is not None and not self.task.done():  # 已在跑
            self.task.cancel()  # 任务级取消（阻塞的 await 立即中断）
        elif self.paused:  # 暂停在 HITL，任务已结束
            self._settle_cancel()  # 专门出口：直接收敛

    def _settle_cancel(self) -> None:  # 暂停态取消收敛
        """WAITING → CANCELLING → CANCELLED，发 cancelled 事件并关流。"""
        ctx = self.ctx  # 上下文
        if ctx.machine.can(RunEvent.CANCEL_REQUEST):  # 可进入取消中
            ctx.transition(RunEvent.CANCEL_REQUEST, reason="user")  # 请求取消
        if ctx.machine.can(RunEvent.CANCEL_ACK):  # 可完成取消
            ctx.transition(RunEvent.CANCEL_ACK, reason="paused")  # 取消完成
        ctx.emit(events.cancelled("user"))  # 事件
        self.paused = False  # 不再暂停
        self.events.put_nowait(SENTINEL)  # 关流

    async def resume(self, value: Any) -> Any:  # HITL 恢复
        """在被暂停的图上用同 thread_id 继续（走 driver，保留 FSM/事件）。"""
        from app.agent.driver import drive as _drive  # 延迟导入

        if self.graph is None:  # 没有可恢复的运行
            raise RuntimeError("该运行未挂载可恢复的图")
        result = await _drive(self.graph, self.ctx, initial_state={}, emit=self.ctx.emit, resume_value=value)  # 恢复
        self.paused = result.status == "waiting"  # 仍暂停？
        if not self.paused:  # 已继续到终态
            self.events.put_nowait(SENTINEL)  # 结束标记
        return result  # 返回


def _graph() -> Any:  # 取父图
    """委托装配器（进程内复用）；保留名字，测试可接桩。"""
    return assembly.build()  # 返回编译图


async def _run(handle: StreamHandle) -> None:  # 后台执行
    """委托驱动器跑图，事件进队列；任何失败都保证 sentinel。

    为**本轮**开一条 AsyncDB 挂到 ctx.db（单一异步写者），随 run 关；供
    load_context 读历史/摘要、persist/trace 写入。仅在真实 worker 单 loop 下验证。
    """
    ctx = handle.ctx  # 上下文
    handle.started = True  # 标记任务已开始（cancel 才能 cancel 任务）
    owned_db = None  # 本轮开的连接
    if ASYNC_DB_ENABLED and ctx.db is None:  # 仅 worker 单 loop 下开（pytest 默认关）
        from app.core import paths  # 数据目录
        from app.core.db.aio import AsyncDB  # 异步连接

        owned_db = AsyncDB(paths.db_file())  # 建（按 run）
        await owned_db.connect()  # 连接（绑定当前 loop）
        ctx.db = owned_db  # 挂到上下文供节点用
    try:  # 委托驱动器
        graph = _graph()  # 取图（恢复入口也要用）
        handle.graph = graph  # 挂到句柄（HITL 恢复）
        handle.thread_id = str(ctx.conversation_id or ctx.run_id)  # 确定性线程 id
        initial = {  # 初始状态
            "question": ctx.question,  # 问题
            "conversation_id": ctx.conversation_id,  # 会话
            "turns": [],  # 历史由 load_context 填
            "attachments": [attachments.public(item) for item in ctx.attachments],  # 附件公开元数据（正文不入 state）
            "run_id": ctx.run_id,  # 运行 id（HITL 负载用）
            "thread_id": handle.thread_id,  # 恢复用线程
            "hitl_enabled": True,  # 生产开启 HITL（clarify 可暂停）
        }  # 状态结束
        result = await drive(graph, ctx, initial_state=initial, emit=ctx.emit)  # 驱动
        handle.run_id = ctx.run_id  # 句柄同步真实 run id
        handle.paused = result.status == "waiting"  # 记录暂停态（供恢复）
    finally:  # 一定结束队列
        if owned_db is not None:  # 本轮开的
            import contextlib  # 抑制

            with contextlib.suppress(Exception):  # 关失败不影响收尾
                await owned_db.close()  # 关
            ctx.db = None  # 摘掉
        if not getattr(handle, "paused", False):  # 暂停时保留流（等待恢复）
            handle.events.put_nowait(SENTINEL)  # 结束标记


def start_stream(  # 启动
    question: str,  # 问题
    *,
    session: str = "",  # 会话
    attachments: list[dict[str, Any]] | None = None,  # 附件
    run_id: str = "",  # 运行 id（路由先生成则复用，保证与发送即落库一致）
) -> StreamHandle:  # 句柄
    """立即返回句柄；后台任务开始跑。"""
    queue: asyncio.Queue = asyncio.Queue()  # 事件队列
    sink = _EventSink(queue)  # 事件出口：终态之后自动闭嘴
    from app.agent.tools.tooling import singleflight_timeouts  # 进程级超时计数

    ctx = RunContext(  # 上下文
        run_id=run_id or uuid.uuid4().hex,  # 复用传入 id，否则生成
        question=question,  # 问题
        conversation_id=session or "default",  # 会话
        emit=sink,  # 事件出口
        attachments=list(attachments or []),  # 附件
        singleflight_base=singleflight_timeouts(),  # 记录基线，收尾算增量
    )  # 上下文结束
    sink._run_id = ctx.run_id  # 事件出口预先知道临时 run id
    handle = StreamHandle(run_id=ctx.run_id, ctx=ctx, events=queue)  # 句柄
    handle.task = asyncio.create_task(_run(handle))  # 后台跑
    return handle  # 返回
