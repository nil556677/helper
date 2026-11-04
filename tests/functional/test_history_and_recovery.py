"""功能测试：历史串答回归、事件契约往返、崩溃对账。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

import pytest  # 测试框架

from app.agent.agent import build_agent_graph  # 父图
from app.agent.context import RunContext  # 上下文
from app.agent.contracts import EventType, from_dict, make, to_dict, validate_sequence  # 事件契约
from app.agent.driver import drive  # 驱动器
from app.agent.middleware.base import ModelResponse  # 响应形状
from app.agent.nodes.agent import node as agent_mod  # 工具循环节点模块
from app.agent.nodes.rag.node import RagResult  # 检索结果
from app.agent.reconcile import reconcile_once  # 对账
from app.core.db import schema_v1  # 建表
from app.core.db.connection import close_thread_connection, connect, get_connection  # 连接
from app.core.trace import store as trace_store  # 运行记录

_EVIDENCE = [  # 一条证据
    {"index": 1, "source_name": "资料", "origin": "段", "text": "证据正文", "snippet": "", "score": 0.1, "score_kind": "rrf"}  # 证据
]  # 列表结束


@pytest.fixture(autouse=True)  # 独立库
def _db(fake_root):
    """建 v1 schema。"""
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关
    yield  # 交给用例
    close_thread_connection()  # 收尾


class _Sink:  # 事件收集
    def __init__(self) -> None:  # 构造
        self.events: list[Any] = []  # 事件
        self._run_id = ""  # run id

    def __call__(self, event: Any) -> None:  # 收
        self.events.append(event)  # 追加


class _Runner:  # 假检索器
    def __init__(self, result: RagResult) -> None:  # 构造
        self._result = result  # 保存

    async def run(self, question: str, *, top_k: int = 6, rerank: bool = True) -> RagResult:  # 跑
        del question, top_k, rerank  # 不用
        return self._result  # 固定


class _RecordingAgent:  # 记录模型输入的假 agent
    """返回固定答案，同时留下最后一次传给 agent 的消息。"""

    def __init__(self) -> None:  # 构造
        self.last_messages: list[Any] = []  # 最近输入
        self.last_config: Any = None  # 最近 config

    async def ainvoke(self, payload: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 执行
        self.last_messages = list((payload or {}).get("messages") or [])  # 记录输入
        self.last_config = config  # 记录 config
        from langchain_core.messages import AIMessage  # 局部导入

        return {"messages": [AIMessage(content="模型答案")]}  # 固定响应


async def _run(question: str, result: RagResult, conversation_id: str) -> None:  # 跑一轮
    graph = build_agent_graph(_Runner(result))  # 父图
    ctx = RunContext(run_id="r", question=question, conversation_id=conversation_id, emit=_Sink())  # 上下文
    initial = {"question": question, "conversation_id": conversation_id, "turns": [], "attachments": []}  # 初始
    await drive(graph, ctx, initial_state=initial)  # 驱动


def _ok() -> RagResult:  # 可答结果
    return RagResult(evidence=_EVIDENCE, hits=[], errors=[], gate={"decision": "pass", "kept": _EVIDENCE})  # 结果


def _reject() -> RagResult:  # 不可答结果
    return RagResult(evidence=[], hits=[], errors=[], gate={"decision": "reject", "kept": []})  # 结果


async def test_unanswered_turn_not_fed_to_next_turn() -> None:  # D1 回归
    """上一轮空回答不得以「悬空提问」进下一轮模型输入。"""
    agent = _RecordingAgent()  # 假 agent
    original_model = agent_mod._model  # 原模型取法
    original_build = agent_mod.build_react_agent  # 原构造
    agent_mod._model = lambda: object()  # 假装模型可用（跳过兜底）
    agent_mod.build_react_agent = lambda *args, **kwargs: agent  # 换假 agent
    try:  # 用完还原
        await _run("广元市下面有哪些县区", _reject(), "conv-x")  # 第一轮：无证据
        await _run("成都下面有哪些区县", _ok(), "conv-x")  # 第二轮：有证据
    finally:  # 还原
        agent_mod._model = original_model  # 还原
        agent_mod.build_react_agent = original_build  # 还原
    texts = [str(getattr(item, "content", item)) for item in agent.last_messages]  # 模型输入文本
    assert not any("广元" in text for text in texts)  # 旧问题不再出现
    assert any("成都下面有哪些区县" in text for text in texts)  # 当前问题仍送
    assert any("成都下面有哪些区县" in text for text in texts)  # 当前问题在


def test_event_roundtrip_and_sequence_validation() -> None:  # 事件契约
    """序列化往返一致；终态唯一且最后。"""
    ack = make(EventType.ACK, run_id="r")  # 回执
    done = make(EventType.DONE, ttft_ms=1, total_ms=2, usage={})  # 终态
    for event in (ack, done):  # 逐个
        assert from_dict(to_dict(event)).type is event.type  # 往返一致
    validate_sequence([ack, done])  # 合法
    with pytest.raises(ValueError):  # 终态不在最后
        validate_sequence([done, ack])  # 非法
    with pytest.raises(ValueError):  # 两个终态
        validate_sequence([ack, done, make(EventType.DONE, ttft_ms=0, total_ms=0)])  # 非法


def test_reconcile_marks_stale_running_interrupted() -> None:  # 崩溃对账
    """残留 running 超时后标 interrupted。"""
    trace_store.create_run("stale-1", question="崩溃任务", kind="agent", started_at=1.0)  # 很久以前
    changed = reconcile_once(stale_s=1)  # 对账
    assert changed == 1  # 改了一条
    row = get_connection().execute("SELECT status, error_kind FROM trace_runs WHERE id = 'stale-1'").fetchone()  # 查
    assert row["status"] == "interrupted"  # 已标
    assert row["error_kind"] == "interrupted"  # 原因
