"""功能测试：聊天主链路（真实父图 + 假检索 + 无模型回退）。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

import pytest  # 测试框架

from app.agent.agent import build_agent_graph  # 父图
from app.agent.context import RunContext  # 上下文
from app.agent.driver import drive  # 驱动器
from app.agent.events import EventType  # 事件类型
from app.agent.nodes.rag.node import RagResult  # 检索结果
from app.core.db import schema_v1  # 建表
from app.core.db.connection import close_thread_connection, connect, get_connection  # 连接

_TERMINALS = {EventType.DONE, EventType.CANCELLED, EventType.ERROR}  # 终态集合

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


@pytest.fixture(autouse=True)  # 每个用例独立库
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
    """固定返回一个 RagResult。"""

    def __init__(self, result: RagResult) -> None:  # 构造
        self._result = result  # 保存

    async def run(self, question: str, *, top_k: int = 6, rerank: bool = True) -> RagResult:  # 跑
        del question, top_k, rerank  # 不用
        return self._result  # 固定结果


def _result(decision: str, evidence: list[dict[str, Any]]) -> RagResult:  # 造结果
    return RagResult(  # 返回
        evidence=evidence,  # 证据
        hits=[],  # 命中
        errors=[],  # 错误
        gate={"decision": decision, "kept": evidence},  # 门限
    )  # 结束


async def _run(question: str, result: RagResult) -> tuple[Any, _Sink]:  # 跑一轮
    """建真实父图并驱动到终态。"""
    graph = build_agent_graph(_Runner(result))  # 父图
    sink = _Sink()  # 事件出口
    ctx = RunContext(run_id="r", question=question, conversation_id="c1", emit=sink)  # 上下文
    initial = {"question": question, "conversation_id": "c1", "turns": [], "attachments": []}  # 初始状态
    return await drive(graph, ctx, initial_state=initial), sink  # 驱动


def _messages() -> list[dict[str, Any]]:  # 读落库消息
    rows = get_connection().execute("SELECT role, content FROM messages ORDER BY id").fetchall()  # 查
    return [dict(row) for row in rows]  # 转字典


async def test_answer_pipeline_uses_extractive_without_model() -> None:  # 有证据主链路
    """无配置模型时回退原文；事件 ack 开头、done 唯一结尾；user+assistant 落库。"""
    result, sink = await _run("问题", _result("pass", _EVIDENCE))  # 跑
    assert result.status == "done"  # 终态
    types = [event.type for event in sink.events]  # 事件序列
    assert types[0] is EventType.ACK  # 先回执
    assert types[-1] is EventType.DONE  # 终态在最后
    assert sum(1 for item in types if item in _TERMINALS) == 1  # 终态唯一
    rows = _messages()  # 落库
    assert [row["role"] for row in rows] == ["user", "assistant"]  # 一轮两消息
    assert "证据正文内容" in rows[1]["content"]  # 原文回退
    trace = get_connection().execute("SELECT status FROM trace_runs ORDER BY started_at DESC LIMIT 1").fetchone()  # 查 trace
    assert trace["status"] == "ok"  # 运行成功


async def test_no_evidence_finalizes_with_note() -> None:  # 无证据链路
    """reject 且无附件：不进生成，落 user+空回答，说明进 note。"""
    result, sink = await _run("没资料的问法", _result("reject", []))  # 跑
    assert result.status == "done"  # 终态
    types = [event.type for event in sink.events]  # 事件
    assert types[-1] is EventType.DONE  # 收尾
    assert sum(1 for item in types if item in _TERMINALS) == 1  # 终态唯一
    rows = _messages()  # 落库
    assert [row["role"] for row in rows] == ["user", "assistant"]  # 一轮两消息
    assert rows[1]["content"] == ""  # 无回答
    turn_json = get_connection().execute("SELECT turn_json FROM messages WHERE role = 'assistant'").fetchone()["turn_json"]  # 查形状
    assert "没有在本地资料里找到相关内容" in (turn_json or "")  # 说明落库


async def test_cancel_before_start_emits_cancelled() -> None:  # 启动前取消
    """取消信号已置位时短路，只发 cancelled 终态。"""
    graph = build_agent_graph(_Runner(_result("pass", _EVIDENCE)))  # 父图
    sink = _Sink()  # 出口
    ctx = RunContext(run_id="r", question="问题", conversation_id="c1", emit=sink)  # 上下文
    ctx.request_cancel()  # 置取消
    result = await drive(graph, ctx, initial_state={"question": "问题", "conversation_id": "c1", "turns": []})  # 驱动
    assert result.status == "cancelled"  # 终态
    assert sink.events[-1].type is EventType.CANCELLED  # 取消事件


def test_no_compat_symbols() -> None:  # 去兼容守卫
    """被删除的旧入口/旧字段不能再回来。"""
    from app.agent import state as agent_state  # 状态
    from app.rag import retrieval  # 检索模块

    assert "answerable" not in agent_state.ChatState.__annotations__  # 双轨字段已删
    assert "history" not in agent_state.ChatState.__annotations__  # 旧历史字段已删
    assert not hasattr(retrieval, "retrieve")  # 旧同步入口已删
    with pytest.raises(ImportError):  # 旧批处理入口已删
        import app.agent.runtime  # noqa: F401  # 期望失败
