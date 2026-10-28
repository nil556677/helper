"""驱动器（E1）：假图驱动 + 终态收敛 + recursion_limit 接线。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 跑异步
from typing import Any  # 任意值

import pytest  # 测试框架
from langgraph.errors import GraphRecursionError  # 递归超限

from app.agent.context import RunContext  # 运行上下文
from app.agent.driver import RECURSION_LIMIT, drive  # 被测
from app.agent.events import EventType  # 事件类型
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect  # 连接


@pytest.fixture(autouse=True)
def _db(fake_root):  # 隔离库
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关


class _Collect:  # 事件收集出口
    def __init__(self) -> None:  # 构造
        self.events: list = []  # 收集
        self._run_id = ""  # 出口 run id（drive 回填）

    def __call__(self, event: Any) -> None:  # 收事件
        self.events.append(event)  # 追加


class _Fake:  # 假图
    def __init__(self, exc: BaseException | None = None, state: dict | None = None) -> None:  # 构造
        self.exc = exc  # 异常
        self.state = state or {}  # 状态
        self.config: dict | None = None  # 记录 config

    async def _record(self, state: dict, config: dict) -> None:  # 记录 config
        del state  # 不在乎输入
        self.config = config  # 记录

    async def ainvoke(self, state: dict, config: dict) -> dict:  # 跑
        """（保留：兼容仅 ainvoke 的调用方。）"""
        await self._record(state, config)  # 记录
        if self.exc is not None:  # 要抛
            raise self.exc  # 抛
        return self.state  # 返回

    async def astream(self, state: dict, config: dict, *, stream_mode=None) -> Any:  # 流式跑
        """driver 现在用 astream；假图产一条 values 快照即可。"""
        del stream_mode  # 不在乎模式
        await self._record(state, config)  # 记录
        if self.exc is not None:  # 要抛
            raise self.exc  # 抛
        yield ("values", dict(self.state))  # 产一条完整快照（假图无节点）


def _drive(  # 跑一次驱动器
    exc: BaseException | None = None,  # 注入异常
    pre_cancel: bool = False,  # 启动前取消
    state: dict | None = None,  # 正常返回的状态
    graph: _Fake | None = None,  # 外部假图（测接线用）
) -> tuple[Any, _Collect, RunContext, _Fake | None]:  # 结果
    sink = _Collect()  # 出口
    ctx = RunContext(run_id="temp", question="问题", conversation_id="c", emit=sink)  # 上下文
    if pre_cancel:  # 启动前取消
        ctx.request_cancel()  # 置位
    target = graph if graph is not None else _Fake(exc, state)  # 图
    result = asyncio.run(  # 同步跑
        drive(target, ctx, initial_state={"question": "问题"}, emit=sink)  # 驱动
    )  # 完成
    return result, sink, ctx, target  # 返回


def test_drive_normal_returns_state() -> None:  # 正常
    result, sink, _ctx, _g = _drive(state={"x": 1})  # 跑
    assert result.status == "done"  # 终态
    assert result.state.get("x") == 1  # 业务字段
    assert any(e.type is EventType.ACK for e in sink.events)  # 发过 ack


def test_drive_seals_business_fields_into_state() -> None:  # 状态单一源
    result, _sink, _ctx, _g = _drive(state={"answer": "答"})  # 跑
    assert result.state["answer"] == "答"  # 答案
    assert result.state["status"] == "done"  # 终态封进 state
    assert result.state["turn_id"] == ""  # 轮次 id 兜底
    assert result.state["attempt"] == 1  # 尝试次数
    assert result.state["usage"] == {}  # 用量兜底
    assert result.state["basis"] == {}  # 依据兜底


def test_drive_passes_recursion_limit() -> None:  # recursion_limit 接线
    result, _sink, _ctx, graph = _drive(graph=_Fake(state={}))  # 跑
    assert result.status == "done"  # 终态
    assert graph is not None and graph.config is not None  # 收到 config
    assert graph.config["recursion_limit"] == RECURSION_LIMIT  # 图步上限


def test_drive_recursion_error_converges_failed() -> None:  # 循环超限
    result, sink, ctx, _g = _drive(exc=GraphRecursionError("loop"))  # 跑
    assert result.status == "failed"  # 终态
    assert isinstance(result.error, GraphRecursionError)  # 错误类型
    assert any(e.type is EventType.ERROR for e in sink.events)  # 错误事件
    assert ctx.persisted is True  # 落了部分结果


def test_drive_generic_failure_returns_failed() -> None:  # 一般失败
    result, sink, _ctx, _g = _drive(exc=RuntimeError("boom"))  # 跑
    assert result.status == "failed"  # 终态
    assert isinstance(result.error, RuntimeError)  # 错误类型
    assert any(e.type is EventType.ERROR for e in sink.events)  # 错误事件


def test_drive_pre_cancel_short_circuits() -> None:  # 启动前取消
    result, sink, _ctx, _g = _drive(pre_cancel=True)  # 跑
    assert result.status == "cancelled"  # 终态
    assert result.state == {}  # 无状态
    assert any(e.type is EventType.CANCELLED for e in sink.events)  # 取消事件
