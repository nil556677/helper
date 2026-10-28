"""回归：历史成对读取 + 检索路由与状态机一致（真实 bug 复现）。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 事件循环
from typing import Any  # 任意值

from app.agent import agent as agent_mod  # 父图（after_rag）
from app.agent.context import RunContext, run_context  # 上下文
from app.agent.contracts import DecisionTarget  # 决策目标
from app.agent.fsm.states import RunEvent, RunState  # 状态机
from app.agent.nodes.context.node import _history  # 被测：历史成对
from app.agent.nodes.rag import entry as entry_mod  # 被测：检索节点
from app.agent.nodes.rag.entry import make_rag_node  # 工厂
from app.agent.nodes.rag.node import RagResult  # 检索结果


def test_history_drops_orphan_user_turns() -> None:  # 历史成对
    """上一轮回答为空时，不能把它的 user 问题单独喂给模型。"""
    rows = [  # 真实故障的数据形状
        {"id": 1, "role": "user", "content": "成都市是哪个省的"},  # 上一轮问题
        {"id": 2, "role": "assistant", "content": ""},  # 回答为空（被过滤）
        {"id": 3, "role": "user", "content": "广元市下面有哪些县区"},  # 又一悬空问题
        {"id": 4, "role": "assistant", "content": ""},  # 回答为空
        {"id": 5, "role": "user", "content": "成都下面有哪些区县"},  # 有配对
        {"id": 6, "role": "assistant", "content": "答案 [1]"},  # 回答
        {"id": 7, "role": "user", "content": "还没回答的新问题"},  # 结尾未配对
    ]  # 数据结束
    items = _history(0, rows)  # 读历史
    assert [item["content"] for item in items] == ["成都下面有哪些区县", "答案 [1]"]  # 只留成对


class _Runner:  # 假检索器
    """固定返回一个 RagResult。"""

    def __init__(self, result: RagResult) -> None:  # 构造
        self._result = result  # 保存

    async def run(self, question: str, *, top_k: int = 6, rerank: bool = True) -> RagResult:  # 跑
        del question, top_k, rerank  # 不用
        return self._result  # 固定结果


class _Planned:  # 假改写结果
    """查询改写固定跳过。"""

    def __init__(self) -> None:  # 构造
        self.queries = ["问题"]  # 查询
        self.reason = "skipped"  # 原因


def _ctx_retrieving() -> RunContext:  # 造 RETRIEVING 上下文
    ctx = RunContext(run_id="r", question="问题")  # 新建
    ctx.transition(RunEvent.START)  # 开始
    ctx.transition(RunEvent.CONTEXT_LOADED)  # 上下文就绪
    return ctx  # 返回


def _run_rag(monkeypatch, result: RagResult) -> tuple[dict[str, Any], RunContext]:  # 跑检索节点
    monkeypatch.setattr(entry_mod.rewrite_mod, "rewrite", lambda question, history=None: _Planned())  # 固定改写
    ctx = _ctx_retrieving()  # 上下文
    node = make_rag_node(_Runner(result))  # 节点

    async def call() -> dict[str, Any]:  # 异步调用
        with run_context(ctx):  # 绑定上下文
            return await node({"question": "问题", "attachments": []}, {"configurable": {"run_ctx": ctx}})  # 跑

    return asyncio.run(call()), ctx  # 返回


def _result(decision: str, evidence: list[dict[str, Any]]) -> RagResult:  # 造结果
    return RagResult(  # 返回
        evidence=evidence,  # 证据
        hits=[],  # 命中
        errors=[],  # 错误
        gate={"decision": decision, "kept": evidence},  # 门限
    )  # 结束


def test_reject_with_partial_evidence_routes_to_finalize(monkeypatch) -> None:  # 关键回归
    """reject 但 kept 非空时，必须收尾，不能路由到 answer（否则状态机非法迁移）。"""
    evidence = [{"index": 1, "text": "只有一条证据", "source_name": "资料", "origin": "段"}]  # 仅一条
    out, ctx = _run_rag(monkeypatch, _result("reject", evidence))  # 跑
    assert out["evidence"] == []  # 证据清空，避免按 evidence 误路由
    assert out["route"]["next"] == DecisionTarget.FINALIZE.value  # 决策对象指向收尾
    assert ctx.machine.state is RunState.FINALIZING  # 状态机已收尾
    assert agent_mod.after_rag(out) == "finalize"  # 路由一致


def test_pass_routes_to_answer(monkeypatch) -> None:  # 正常路径
    """pass 且证据非空时进入生成。"""
    evidence = [{"index": 1, "text": "证据", "source_name": "资料", "origin": "段"}]  # 证据
    out, ctx = _run_rag(monkeypatch, _result("pass", evidence))  # 跑
    assert out["evidence"] == evidence  # 证据保留
    assert out["route"]["next"] == DecisionTarget.ANSWER.value  # 决策对象指向生成
    assert ctx.machine.state is RunState.GENERATING  # 进入生成
    assert agent_mod.after_rag(out) == "answer"  # 路由一致


def test_after_rag_reads_route_only() -> None:  # 单一事实来源
    """即使 evidence 残留，也只按 route 决策（不再有第二信号）。"""
    assert agent_mod.after_rag({"route": {"next": "finalize"}, "evidence": [{"x": 1}]}) == "finalize"  # 收尾
