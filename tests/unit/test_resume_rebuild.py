"""任务7 跨重启续跑：重建 handle + LocalBackend 缺 handle 降级。"""

from __future__ import annotations

import asyncio  # 异步

import pytest  # 测试框架

from app.agent import stream as stream_mod  # 流式入口
from app.agent.fsm.states import RunState  # 状态
from app.agent.run.local import LocalBackend  # worker 后端


class _Graph:  # 桩图：记录入参，产一条 values（到"恢复后完成"）
    def __init__(self) -> None:  # 构造
        self.received = None  # 收到的图入参（应是 Command(resume=值)）
        self.runs = 0  # astream 被调次数

    async def astream(self, graph_input, config, *, stream_mode=None):  # 流式入口
        del config, stream_mode  # 不用
        self.received = graph_input  # 记入参
        self.runs += 1  # 计次
        yield ("values", {"answer": "ok", "fragments": []})  # 一条完整快照


class _Detail:  # 桩 run 记录
    conversation_id = "conv-7"  # 会话
    question = "原问题"  # 原问题


def _run_detail(monkeypatch) -> None:  # 桩 trace_store.detail
    monkeypatch.setattr("app.core.trace.store.detail", lambda run_id: _Detail())  # 反查


def test_resume_paused_run_rebuilds_and_resumes(fake_root, monkeypatch) -> None:  # 核心重建+恢复
    _run_detail(monkeypatch)  # 桩反查
    graph = _Graph()  # 桩图

    async def go():  # 异步壳
        handle = await stream_mod.resume_paused_run("run9", "北京", graph=graph)  # 重建恢复
        got = []  # 事件
        while True:  # 收
            event = await handle.events.get()  # 取
            if event is stream_mod.SENTINEL:  # 结束
                break  # 停
            got.append(event)  # 记
        return handle, got  # 返回

    handle, got = asyncio.run(go())  # 跑
    assert handle.ctx.conversation_id == "conv-7"  # 反查会话成功
    assert handle.ctx.machine.state is RunState.GENERATING  # seed WAITING → resume → GENERATING（非误走 START）
    assert graph.runs == 1  # 图只跑一次
    assert getattr(graph.received, "resume", None) == "北京"  # 确实以 Command(resume="北京") 驱动
    assert any(getattr(e, "type", None) == "ack" for e in got)  # 有回执
    assert handle.task is not None and handle.task.done()  # 后台恢复任务已结束


def test_resume_without_trace_raises(fake_root, monkeypatch) -> None:  # 反查不到 → 不可恢复
    monkeypatch.setattr("app.core.trace.store.detail", lambda run_id: None)  # 无记录

    async def go():  # 异步壳
        return await stream_mod.resume_paused_run("ghost", "x", graph=_Graph())  # 应抛

    with pytest.raises(RuntimeError):  # 期望不可恢复
        asyncio.run(go())  # 跑


def test_localbackend_resume_rebuilds_when_handle_missing(fake_root, monkeypatch) -> None:  # 缺 handle 降级重建
    async def _fake(run_id, value, **kw):  # 桩重建入口（async，免得真建 checkpoint 图）
        return _Captured(run_id, value)  # 假装重建好的 handle

    monkeypatch.setattr("app.agent.stream.resume_paused_run", _fake)  # 替换重建入口
    backend = LocalBackend(factory=lambda *a, **k: None)  # 空后端（无 handle）
    asyncio.run(backend.resume("run9", "上海"))  # 缺 handle → 走重建
    captured = backend._handles.get("run9")  # 重建结果
    assert isinstance(captured, _Captured)  # 已被登记
    assert captured.run_id == "run9" and captured.value == "上海"  # 参数透传


class _Captured:  # 记录重建调用参数的桩
    def __init__(self, run_id, value) -> None:  # 构造
        self.run_id = run_id  # run
        self.value = value  # 值