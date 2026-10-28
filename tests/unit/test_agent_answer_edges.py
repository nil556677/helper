"""answer 节点异常路径：无证据、模型失败、空流、取消。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 事件循环

import pytest  # 测试框架

from app.agent.context import RunCancelled, RunContext  # 上下文
from app.agent.fsm.states import RunEvent  # 状态事件
from app.agent.middleware.base import ModelResponse  # 响应形状
from app.agent.nodes.answer import node as answer_mod  # 被测模块

EVIDENCE = [  # 一条证据
    {  # 证据字典
        "index": 1,  # 序号
        "heading": "标题",  # 标题
        "origin": "第 1 段",  # 出处
        "source_name": "资料",  # 来源
        "score": 0.1,  # 分数
        "score_kind": "rrf",  # 分数语义
        "text": "证据正文",  # 正文
        "snippet": "证据",  # 命中段
    }  # 证据结束
]  # 列表结束


def _ctx() -> RunContext:  # 造上下文
    """进入 GENERATING 状态。"""
    ctx = RunContext(run_id="r", question="q")  # 新建
    ctx.transition(RunEvent.START)  # 开始
    ctx.transition(RunEvent.CONTEXT_LOADED)  # 上下文
    ctx.transition(RunEvent.RETRIEVAL_DONE)  # 有证据
    return ctx  # 返回


class _Chain:  # 假中间件链
    """按需返回响应/抛异常。"""

    def __init__(self, *, error: BaseException | None = None, stream: bool = False, empty: bool = False, text: str = "答案 [1]") -> None:  # 构造
        self.error = error  # 异常
        self.stream = stream  # 是否流式
        self.empty = empty  # 是否空流
        self.text = text  # 固定文本

    async def model_call(self, ctx, request, call):  # 调模型
        """返回固定响应。"""
        if self.error is not None:  # 抛错
            raise self.error  # 抛出
        if self.empty:  # 空流
            async def empty_stream():  # 生成器
                """什么都不吐。"""
                if False:  # 永远不成立
                    yield ""  # 占位
            return ModelResponse(stream=empty_stream())  # 空流
        if self.stream:  # 流式
            async def tokens():  # 生成器
                """先吐一段，然后请求取消。"""
                yield "答案"  # 第一段
                ctx.request_cancel()  # 请求取消
                yield " [1]"  # 第二段
            return ModelResponse(stream=tokens())  # 流
        return ModelResponse(text=self.text)  # 非流式


def test_no_evidence_returns_note(fake_root) -> None:  # 无证据
    """不生成，给说明。"""
    ctx = _ctx()  # 上下文
    result = asyncio.run(answer_mod.answer_node({"question": "q", "evidence": []}, {"configurable": {"run_ctx": ctx}}))  # 跑
    assert result.get("note")  # 有说明
    assert ctx.partial_answer == ""  # 没生成


def test_model_failure_falls_back(fake_root, monkeypatch) -> None:  # 模型失败
    """回退资料原文。"""
    monkeypatch.setattr(answer_mod, "_CHAIN", _Chain(error=RuntimeError("boom")))  # 假链
    ctx = _ctx()  # 上下文
    result = asyncio.run(answer_mod.answer_node({"question": "q", "evidence": EVIDENCE}, {"configurable": {"run_ctx": ctx}}))  # 跑
    assert "证据正文" in str(result.get("answer"))  # 原文
    assert ctx.note  # 有说明


def test_empty_stream_returns_note(fake_root, monkeypatch) -> None:  # 空流
    """模型没吐内容时不能让界面空白。"""
    monkeypatch.setattr(answer_mod, "_CHAIN", _Chain(empty=True))  # 假链
    ctx = _ctx()  # 上下文
    result = asyncio.run(answer_mod.answer_node({"question": "q", "evidence": EVIDENCE}, {"configurable": {"run_ctx": ctx}}))  # 跑
    assert ctx.note == "模型没有返回内容，请稍后重试。"  # 说明
    assert result.get("note")  # 写回


def test_conflict_marker_is_split(fake_root, monkeypatch) -> None:  # 冲突标记
    """资料冲突标记不进正文，单独提示。"""
    monkeypatch.setattr(answer_mod, "_CHAIN", _Chain(text="答案 C [1]\n[[资料冲突]] 资料答案与通用知识不一致"))  # 假链
    ctx = _ctx()  # 上下文
    result = asyncio.run(answer_mod.answer_node({"question": "q", "evidence": EVIDENCE}, {"configurable": {"run_ctx": ctx}}))  # 跑
    assert "[[资料冲突]]" not in str(result.get("answer"))  # 正文干净
    assert ctx.conflict == "资料答案与通用知识不一致"  # 提示保留


def test_cancel_during_stream(fake_root, monkeypatch) -> None:  # 流中取消
    """取消应抛出，且保留已生成部分。"""
    monkeypatch.setattr(answer_mod, "_CHAIN", _Chain(stream=True))  # 假链
    ctx = _ctx()  # 上下文
    with pytest.raises(RunCancelled):  # 期望取消
        asyncio.run(answer_mod.answer_node({"question": "q", "evidence": EVIDENCE}, {"configurable": {"run_ctx": ctx}}))  # 跑
    assert ctx.partial_answer == "答案"  # 保留第一段
