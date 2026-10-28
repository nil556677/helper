"""功能测试：driver 收权——状态机迁移由 driver 通过投影驱动。

回归目标（Q16/Q3）：节点不再自行 ``ctx.transition``；driver 观测节点完成后，用
``fsm/projection.py`` 把「完成节点 + route」投影成 ``RunEvent`` 并统一驱动迁移。
本测试驱动真实父图，断言 STATE 事件序列与运行生命周期一致，从而证明：状态迁移
的唯一推进者是 driver，节点只返回 state update。
"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

import pytest  # 测试框架

from app.agent.agent import build_agent_graph  # 父图
from app.agent.context import RunContext  # 上下文
from app.agent.driver import drive  # 驱动器
from app.agent.events import EventType  # 事件类型
from app.agent.middleware.base import ModelResponse  # 响应形状
from app.agent.nodes.answer import node as answer_mod  # 生成节点（换假链）
from app.agent.nodes.rag.node import RagResult  # 检索结果
from app.core.db import schema_v1  # 建表
from app.core.db.connection import close_thread_connection, connect  # 连接

_EVIDENCE = [  # 一条证据
    {  # 证据
        "index": 1,  # 序号
        "source_name": "资料",  # 来源
        "origin": "第 1 段",  # 出处
        "heading": "标题",  # 标题
        "text": "证据正文内容",  # 正文
        "snippet": "证据",  # 命中段
        "score": 0.1,  # 分数
        "score_kind": "rrf",  # 分数语义
    }  # 结束
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
        self._run_id = ""  # 出口 run id

    def __call__(self, event: Any) -> None:  # 收事件
        self.events.append(event)  # 追加


class _Runner:  # 假检索器
    def __init__(self, result: RagResult) -> None:  # 构造
        self._result = result  # 保存

    async def run(self, question: str, *, top_k: int = 6, rerank: bool = True) -> RagResult:  # 跑
        del question, top_k, rerank  # 不用
        return self._result  # 固定


class _Chain:  # 假模型链
    async def model_call(self, ctx, request, call):  # 调模型
        del ctx, request, call  # 不用
        return ModelResponse(text="模型答案")  # 固定


def _state_path(events: list[Any]) -> list[str]:  # 提取 state 事件 after 序列
    """从事件流抽「after」状态，断言运行生命周期。"""
    return [  # 列表
        event.payload.after  # 迁移后
        for event in events  # 每条
        if event.type is EventType.STATE  # 只要状态
    ]  # 结束


async def _run(result: RagResult) -> _Sink:  # 跑一轮
    """真实父图 + 假检索 + 假模型，收集事件。"""
    orig = answer_mod._CHAIN  # 存原链
    answer_mod._CHAIN = _Chain()  # 换假链
    try:  # 用完还原
        graph = build_agent_graph(_Runner(result))  # 父图
        sink = _Sink()  # 事件出口
        ctx = RunContext(run_id="r", question="问题", conversation_id="c1", emit=sink)  # 上下文
        initial = {"question": "问题", "conversation_id": "c1", "turns": [], "attachments": []}  # 初始
        result_drive = await drive(graph, ctx, initial_state=initial)  # 驱动
        assert result_drive.status == "done"  # 终态正常
        return sink  # 事件
    finally:  # 还原
        answer_mod._CHAIN = orig  # 还原


async def test_driver_drives_state_machine_on_answer_path() -> None:  # 有证据链路
    """可答：状态机走到 DONE，事件序列覆盖完整生命周期。"""
    result = RagResult(evidence=_EVIDENCE, hits=[], errors=[], gate={"decision": "pass", "kept": _EVIDENCE})  # 结果
    sink = await _run(result)  # 跑
    path = _state_path(sink.events)  # 状态序列
    # START→context_loading → CONTEXT_LOADED→retrieving → RETRIEVAL_DONE→generating
    # → GENERATION_DONE→finalizing → FINALIZED→done
    assert path == ["context_loading", "retrieving", "generating", "finalizing", "done"]  # 完整
    assert sink.events[-1].type is EventType.STATE or sink.events[-1].type is EventType.DONE  # 终态收尾
    assert sink.events[-1].type is EventType.DONE  # done 是最后一帧


async def test_driver_drives_state_machine_on_no_evidence_path() -> None:  # 无证据链路
    """不可答：从 retrieving 直接 finalizing（跳过 generating）。"""
    result = RagResult(evidence=[], hits=[], errors=[], gate={"decision": "reject", "kept": []})  # 结果
    sink = await _run(result)  # 跑
    path = _state_path(sink.events)  # 状态序列
    assert path == ["context_loading", "retrieving", "finalizing", "done"]  # 无 generating


def test_projection_is_pure_and_total_for_known_nodes() -> None:  # 投影自洽
    """已知节点一定投影成迁移事件；节点与迁移一一对应。"""
    from app.agent.contracts import DecisionTarget  # 决策目标
    from app.agent.fsm.projection import project_event  # 被测
    from app.agent.fsm.states import RunEvent  # 迁移事件

    assert project_event("load_context", None) is RunEvent.CONTEXT_LOADED  # 固定映射
    assert project_event("answer", None) is RunEvent.GENERATION_DONE  # 固定映射
    assert project_event("finalize", None) is RunEvent.FINALIZED  # 固定映射
    assert project_event("rag", {"route": {"next": DecisionTarget.ANSWER.value}}) is RunEvent.RETRIEVAL_DONE  # 走 answer
    assert project_event("rag", {"route": {"next": DecisionTarget.FINALIZE.value}}) is RunEvent.NO_EVIDENCE  # 走收尾
    assert project_event("unknow_node", {}) is None  # 未知节点不驱动
